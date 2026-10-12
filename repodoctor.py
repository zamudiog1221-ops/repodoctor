#!/usr/bin/env python3
"""repodoctor: read a repo's setup requirements from its code and check them against this machine."""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

OK, FAIL, WARN = "ok", "fail", "warn"
MARK = {OK: ("✓", "32"), FAIL: ("✗", "31"), WARN: ("!", "33")}

SKIP_DIRS = {"node_modules", "venv", "env", "dist", "build", "out", "target", "vendor", "coverage",
             "__pycache__", "site-packages", "test", "tests", "__tests__", "e2e", "docs", "examples"}
SKIP_FILE = re.compile(r"(^test_|^conftest\.|_test\.|\.test\.|\.spec\.|\.min\.|^(playwright|jest|vitest|cypress)\.config\.)")
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Variables the shell, the runtime or the host platform sets; nobody puts these in .env.
AMBIENT = {"NODE_ENV", "PATH", "HOME", "USER", "PWD", "SHELL", "TERM", "LANG", "TZ", "EDITOR", "CI",
           "TMPDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOSTNAME", "HOST", "PORT",
           "DEBUG", "PYTHONPATH", "VIRTUAL_ENV", "NO_COLOR", "FORCE_COLOR", "VERCEL", "NETLIFY",
           "MODE", "DEV", "PROD", "SSR", "BASE_URL"}
AMBIENT_PREFIX = ("npm_", "GITHUB_", "VERCEL_", "XDG_", "RUNNER_")

# The trailing group marks a use that copes with the variable being unset: a fallback, a ternary or
# && test, or an assignment.
JS_ENV = re.compile(r"""(?:process\.env|import\.meta\.env)(?:\.([A-Z][A-Z0-9_]*)|\[\s*['"](\w+)['"]\s*\])"""
                    r"""(\s*(?:\|\||\?|&&|=(?!=)))?""")
JS_DESTRUCT = re.compile(r"\{([^}]*)\}\s*=\s*process\.env\b(?!\s*[.\[])")
PY_INDEX = re.compile(r"""\benviron\[\s*['"](\w+)['"]\s*\](\s*=(?!=))?""")
PY_GET = re.compile(r"""\b(?:environ\.get|getenv)\(\s*['"](\w+)['"]\s*(,|\)\s*or\b)?""")

ENV_FILES = [".env", ".env.local", ".env.development", ".env.development.local"]
ENV_EXAMPLES = [".env.example", ".env.sample", ".env.template", ".env.dist"]

NODE_LOCKS = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")]
PY_LOCKS = [("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile", "pipenv")]
INSTALL = {"pnpm": "npm install -g pnpm", "yarn": "npm install -g yarn", "bun": "npm install -g bun",
           "uv": "pip install uv", "poetry": "pipx install poetry", "pipenv": "pip install pipenv"}

SERVICES = {5432: "Postgres", 6379: "Redis", 27017: "MongoDB", 3306: "MySQL"}
NODE_DRIVERS = {"pg": 5432, "postgres": 5432, "redis": 6379, "ioredis": 6379, "mongodb": 27017,
                "mongoose": 27017, "mysql": 3306, "mysql2": 3306}
PY_DRIVERS = {"psycopg2-binary": 5432, "psycopg2": 5432, "psycopg": 5432, "asyncpg": 5432, "redis": 6379,
              "pymongo": 27017, "motor": 27017, "mysqlclient": 3306, "pymysql": 3306}
PY_DRIVER = re.compile(r"""(?mi)(?:^|["'])\s*(%s)\s*(?=$|[\["'<>=~!;,\s])""" % "|".join(PY_DRIVERS))
# ponytail: pattern match, not a YAML parser; misses ports written as ${VAR:-5432}:5432. Parse YAML if that bites.
COMPOSE_PORT = re.compile(r"""(?m)^\s*-\s*["']?(?:[\d.]+:)?(\d+):(\d+)(?:/tcp)?["']?\s*$""")


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def first_line(path):
    return (read(path).strip().splitlines() or [""])[0].strip()


def version(text):
    m = re.search(r"\d+(?:\.\d+)*", text or "")
    return tuple(int(p) for p in m.group().split(".")) if m else None


def satisfies(found, spec, n):
    """True if version tuple `found` meets `spec`. A bare version pins its first n parts."""
    found = tuple(found) + (0, 0)

    def part_ok(part):
        req = version(part)
        if not req:
            return True
        op = re.match(r"[<>=^~!]*", part).group()
        if op in (">=", ">"):
            return found >= req
        if op == "<":
            return found[:len(req)] < req
        if op == "<=":
            return found[:len(req)] <= req
        if op == "!=":
            return True
        if op in ("^", "~", "~="):  # ponytail: same major and not older; tighter ~ rules not modelled
            return found[0] == req[0] and found >= req
        k = min(n, len(req))
        return found[:k] == req[:k]

    alts = [re.sub(r"([<>=^~!])\s+", r"\1", a) for a in spec.split("||")]
    return any(all(part_ok(p) for p in re.split(r"[,\s]+", a) if p) for a in alts if version(a))


def run_version(*cmds):
    """Version tuple of the first of `cmds` found on PATH, or None."""
    for cmd in cmds:
        exe = shutil.which(cmd)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        found = version(out.stdout + out.stderr)
        if found:
            return found
    return None


def on_path(cmd):
    return shutil.which(cmd) is not None


def port_open(port):
    try:
        socket.create_connection(("localhost", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def env_uses(line, is_js):
    """Yield (name, optional) for each env var read on this line."""
    if is_js:
        for m in JS_ENV.finditer(line):
            yield m.group(1) or m.group(2), bool(m.group(3))
        for m in JS_DESTRUCT.finditer(line):
            for item in m.group(1).split(","):
                name = re.match(r"\s*(\w+)", item)
                if name:
                    yield name.group(1), "=" in item
    else:
        for rx in (PY_INDEX, PY_GET):
            for m in rx.finditer(line):
                yield m.group(1), bool(m.group(2))


def scan_env(root):
    """Map each env var the source reads without a fallback to the 'file:line' of its first use."""
    required = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            ext = os.path.splitext(name)[1]
            if (ext not in JS_EXT and ext != ".py") or SKIP_FILE.search(name):
                continue
            path = Path(dirpath, name)
            try:
                if path.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            for n, line in enumerate(read(path).splitlines(), 1):
                if "env" not in line or line.lstrip().startswith(("//", "#", "*")):
                    continue
                for var, optional in env_uses(line, ext in JS_EXT):
                    if not optional and var not in AMBIENT and not var.startswith(AMBIENT_PREFIX):
                        required.setdefault(var, f"{path.relative_to(root).as_posix()}:{n}")
    return required


def env_keys(root, names, commented=False):
    """Variable names defined in the given env files; `commented` also counts '# KEY=' lines."""
    rx = re.compile(r"^\s*%s(?:export\s+)?(\w+)\s*=" % (r"#?\s*" if commented else ""), re.M)
    return {key for name in names for key in rx.findall(read(root / name))}


def check_version(label, found, spec, source, n, fix):
    need = f"{label} {spec} required ({source})"
    if not found:
        return FAIL, f"{need}, but {label} was not found on PATH", fix
    shown = ".".join(map(str, found))
    if satisfies(found, spec, n):
        return OK, f"{need}, found {shown}", None
    return FAIL, f"{need}, found {shown}", fix


def diagnose(root, ci=False, env=os.environ):
    """Return a list of (status, message, fix) for the repo at `root`."""
    root = Path(root)
    checks = []

    def add(*check):
        checks.append(check)

    required = scan_env(root)
    example = next((n for n in ENV_EXAMPLES if (root / n).exists()), None)
    documented = env_keys(root, ENV_EXAMPLES, commented=True)
    undocumented = sorted(v for v in required if v not in documented)

    if ci:
        for var in undocumented:
            add(FAIL, f"{var} is read at {required[var]} but is not in {example or '.env.example'}",
                f"add {var}= to {example or '.env.example'}")
        if required and not undocumented:
            add(OK, f"All {len(required)} environment variables are documented in {example}", None)
        return checks

    try:
        pkg = json.loads(read(root / "package.json") or "{}")
    except ValueError:
        pkg = {}
    if not isinstance(pkg, dict):
        pkg = {}
    pyproject = read(root / "pyproject.toml")
    is_node = (root / "package.json").exists()
    is_py = (any((root / f).exists() for f in ("requirements.txt", "Pipfile", "setup.py", "uv.lock",
                                               "poetry.lock", ".python-version"))
             or bool(re.search(r"(?m)^\[(project|tool\.poetry)\]", pyproject)))

    if is_node:
        node = run_version("node")
        engines = pkg.get("engines")
        specs = [(".nvmrc", first_line(root / ".nvmrc")), (".node-version", first_line(root / ".node-version")),
                 ("package.json engines", str(engines.get("node", "")) if isinstance(engines, dict) else "")]
        for source, spec in specs:
            if version(spec):
                add(*check_version("Node", node, spec, source, 1, f"nvm install {version(spec)[0]}"))
                break
        else:
            if not node:
                add(FAIL, "Node is needed (package.json) but was not found on PATH",
                    "install Node from https://nodejs.org")
        pm = (str(pkg.get("packageManager") or "").split("@")[0]
              or next((p for f, p in NODE_LOCKS if (root / f).exists()), "npm"))
        if pm != "npm" and not on_path(pm):
            add(FAIL, f"{pm} is this repo's package manager but is not installed", INSTALL.get(pm))
        if pkg.get("dependencies") or pkg.get("devDependencies"):
            if (root / "node_modules").is_dir():
                add(OK, "Node dependencies installed (node_modules exists)", None)
            else:
                add(FAIL, "Node dependencies not installed (no node_modules folder)", f"{pm} install")

    if is_py:
        pm = next((p for f, p in PY_LOCKS if (root / f).exists()), None)
        py_spec = re.search(r"""(?m)^(?:requires-python|python)\s*=\s*["']([^"']+)""", pyproject)
        specs = [(".python-version", first_line(root / ".python-version")),
                 ("runtime.txt", first_line(root / "runtime.txt")),
                 ("pyproject.toml", py_spec.group(1) if py_spec else "")]
        if pm != "uv":  # uv downloads the right Python by itself
            python = run_version("python", "python3") or tuple(sys.version_info[:3])
            for source, spec in specs:
                if version(spec):
                    want = ".".join(map(str, version(spec)[:2]))
                    add(*check_version("Python", python, spec, source, 2,
                                       f"install Python {want} (for example: pyenv install {want})"))
                    break
        if pm and not on_path(pm):
            add(FAIL, f"{pm} is this repo's package manager but is not installed", INSTALL.get(pm))
        # poetry and pipenv keep virtualenvs outside the repo, so there's nothing to look for.
        if pm in (None, "uv") and not env.get("VIRTUAL_ENV") \
                and not any((root / d).is_dir() for d in (".venv", "venv", "env")):
            install = ("uv sync" if pm == "uv" else "python -m venv .venv, activate it, then pip install "
                       + ("-r requirements.txt" if (root / "requirements.txt").exists() else "-e ."))
            add(WARN, "No virtual environment found (.venv or venv), so dependencies are probably not installed",
                install)

    set_keys = env_keys(root, ENV_FILES)
    missing = sorted(v for v in required if v not in env and v not in set_keys)
    if required and not missing:
        add(OK, f"All {len(required)} required environment variables are set", None)
    # .env.example is a declared requirement even when the code reads env vars in a way the scan can't see.
    declared_unset = sorted(k for k in env_keys(root, ENV_EXAMPLES) if k not in env and k not in set_keys)
    if declared_unset and not any((root / n).exists() for n in ENV_FILES):
        add(FAIL, f"No .env file; {example} defines {len(declared_unset)} variable(s)",
            f"cp {example} .env, then fill in the values")
        missing = [v for v in missing if v not in documented]
    elif set(declared_unset) - set(missing):
        add(WARN, f"In {example} but not in your .env: " + ", ".join(sorted(set(declared_unset) - set(missing))),
            f"copy them over from {example} if you need them")
    for var in missing:
        add(FAIL, f"{var} is not set (first used in {required[var]})", f"add {var}=... to .env")
    if undocumented and not example:
        add(WARN, f"No .env.example file; {len(undocumented)} variable(s) the code reads are undocumented",
            "create .env.example listing: " + ", ".join(undocumented))
    else:
        for var in undocumented:
            add(WARN, f"{var} is used in code ({required[var]}) but missing from {example}",
                f"add {var}= to {example}")

    declared = {}
    compose = next((n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
                    if (root / n).exists()), None)
    if compose:
        override = compose.replace(".", ".override.")  # Docker merges this file in automatically
        for host, container in COMPOSE_PORT.findall(read(root / compose) + "\n" + read(root / override)):
            if int(container) in SERVICES:
                declared[int(host)] = SERVICES[int(container)]
    for port, service in sorted(declared.items()):
        if port_open(port):
            add(OK, f"{service} reachable on localhost:{port}", None)
        else:
            add(FAIL, f"{service} ({compose}) is not reachable on localhost:{port}",
                "docker compose up -d" if on_path("docker") else "install Docker, then: docker compose up -d")
    drivers = {}
    for dep in list(pkg.get("dependencies") or {}) + list(pkg.get("devDependencies") or {}):
        if dep in NODE_DRIVERS:
            drivers.setdefault(NODE_DRIVERS[dep], dep)
    for text in (read(root / "requirements.txt"), pyproject, read(root / "Pipfile")):
        for dep in PY_DRIVER.findall(text):
            drivers.setdefault(PY_DRIVERS[dep.lower()], dep)
    for port, dep in sorted(drivers.items()):
        service = SERVICES[port]
        if service in declared.values():
            continue
        if port_open(port):
            add(OK, f"{service} reachable on localhost:{port}", None)
        else:  # a warning, not a failure: the database may be remote
            add(WARN, f"{dep} is a dependency, but no {service} is listening on localhost:{port}",
                f"start {service} locally, or ignore this if yours is remote")
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(prog="repodoctor", description=__doc__.split(": ", 1)[1])
    ap.add_argument("path", nargs="?", default=".", help="repo to check (default: current folder)")
    ap.add_argument("--ci", action="store_true",
                    help="only check that every env var the code reads is in .env.example (for pipelines)")
    args = ap.parse_args(argv)
    if not Path(args.path).is_dir():
        ap.error(f"not a folder: {args.path}")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if color and os.name == "nt":
        os.system("")  # switches older Windows consoles to ANSI colour mode

    checks = diagnose(args.path, ci=args.ci)
    if not checks:
        print("repodoctor: " + ("the code reads no environment variables that need documenting" if args.ci
                                else "nothing to check here (no Node or Python requirements found)"))
        return 0
    print(f"repodoctor: {len(checks)} check{'s' * (len(checks) != 1)}\n")
    for status, message, fix in checks:
        mark, code = MARK[status]
        print(f"  \033[{code}m{mark}\033[0m {message}" if color else f"  {mark} {message}")
        if fix:
            print(f"      fix: {fix}")
    count = {s: sum(c[0] == s for c in checks) for s in (FAIL, WARN, OK)}
    print(f"\n{count[FAIL]} failed, {count[WARN]} warning(s), {count[OK]} passed")
    return 1 if count[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
