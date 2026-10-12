#!/usr/bin/env python3
"""setupdoctor: read a repo's setup requirements from its code and check them against this machine."""
import argparse
import ast
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import warnings
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
           "MODE", "DEV", "PROD", "SSR", "BASE_URL", "PROGRAMFILES", "SYSTEMROOT", "WINDIR", "COMSPEC", "OS"}
AMBIENT_PREFIX = ("npm_", "GITHUB_", "VERCEL_", "XDG_", "RUNNER_")

# A read is "hard" when the code breaks without the variable: an unconditional os.environ["X"], or
# TypeScript's process.env.X! assertion. It is "optional" when the code copes: a fallback, a ternary
# or && test, or an assignment. Everything else (os.getenv("X"), a bare process.env.X, any read inside
# an if or try) is treated as a setting the app can run without, because on real repos that is what
# it nearly always is.
JS_ENV = re.compile(r"""(?:process\.env|import\.meta\.env)(?:\.([A-Z][A-Z0-9_]*)|\[\s*['"](\w+)['"]\s*\])"""
                    r"""(!(?!=))?(\s*(?:\|\||\?|&&|=(?!=)))?""")
JS_DESTRUCT = re.compile(r"\{([^}]*)\}\s*=\s*process\.env\b(?!\s*[.\[])")

ENV_FILES = [".env", ".env.local", ".env.development", ".env.development.local"]
ENV_EXAMPLES = [".env.example", ".env.sample", ".env.template", ".env.dist"]

NODE_LOCKS = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")]
PY_LOCKS = [("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile", "pipenv")]
INSTALL = {"pnpm": "npm install -g pnpm", "yarn": "npm install -g yarn", "bun": "npm install -g bun",
           "uv": "pip install uv", "poetry": "pipx install poetry", "pipenv": "pip install pipenv"}

SERVICES = {5432: "Postgres", 6379: "Redis", 27017: "MongoDB", 3306: "MySQL"}
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


def js_env_uses(text):
    """Yield (name, line number, optional, hard) for each env var a JS/TS file reads."""
    for n, line in enumerate(text.splitlines(), 1):
        # long lines are minified or generated code
        if "env" not in line or len(line) > 2000 or line.lstrip().startswith(("//", "*")):
            continue
        for m in JS_ENV.finditer(line):
            yield m.group(1) or m.group(2), n, bool(m.group(4)), bool(m.group(3))
        for m in JS_DESTRUCT.finditer(line):
            for item in m.group(1).split(","):
                name = re.match(r"\s*(\w+)", item)
                if name:
                    yield name.group(1), n, "=" in item, False


def py_env_uses(text):
    """Return (name, line number, optional, hard) for each env var a Python file reads."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []  # a template, or syntax this Python can't read: nothing can be said safely

    def is_environ(node):
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or \
               (isinstance(node, ast.Name) and node.id == "environ")

    def text_of(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    uses, stack = [], [(tree, False, False)]
    while stack:
        node, guarded, in_or = stack.pop()
        if isinstance(node, ast.Subscript) and is_environ(node.value) and text_of(node.slice) \
                and isinstance(node.ctx, ast.Load):
            uses.append((text_of(node.slice), node.lineno, False, not guarded))
        elif isinstance(node, ast.Call) and node.args and text_of(node.args[0]):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "getenv") or (isinstance(f, ast.Attribute) and (
                    f.attr == "getenv" or (f.attr == "get" and is_environ(f.value)))):
                has_default = len(node.args) > 1 or bool(node.keywords) or in_or
                uses.append((text_of(node.args[0]), node.lineno, has_default, False))
        guarded = guarded or isinstance(node, (ast.If, ast.IfExp, ast.Try))
        is_or = isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        stack.extend((child, guarded, is_or) for child in ast.iter_child_nodes(node))
    return sorted(uses, key=lambda use: use[1])


def scan_env(root):
    """Map each env var the source reads without a fallback to ('file:line' of first use, hard)."""
    reads = {}
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
            text = read(path)
            if "env" not in text:
                continue
            for var, n, optional, hard in (py_env_uses if ext == ".py" else js_env_uses)(text):
                if optional or var in AMBIENT or var.startswith(AMBIENT_PREFIX):
                    continue
                if var not in reads or (hard and not reads[var][1]):
                    reads[var] = (f"{path.relative_to(root).as_posix()}:{n}", hard)
    return reads


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

    reads = scan_env(root)
    example = next((n for n in ENV_EXAMPLES if (root / n).exists()), None)
    documented = env_keys(root, ENV_EXAMPLES, commented=True)
    undocumented = sorted(v for v in reads if v not in documented)

    if ci:
        for var in undocumented:
            add(FAIL, f"{var} is read at {reads[var][0]} but is not in {example or '.env.example'}",
                f"add {var}= to {example or '.env.example'}")
        if reads and not undocumented:
            add(OK, f"All {len(reads)} environment variables are documented in {example}", None)
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
    required = [v for v, (_, hard) in reads.items() if hard]
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
        add(FAIL, f"{var} is not set (first used in {reads[var][0]})", f"add {var}=... to .env")
    # Only worth a line when the repo keeps an example file and it has fallen behind the code.
    if undocumented and example:
        shown = ", ".join(undocumented[:5]) + (f" and {len(undocumented) - 5} more" if len(undocumented) > 5 else "")
        add(WARN, f"{len(undocumented)} variable(s) the code reads are missing from {example}: {shown}",
            "setupdoctor --ci lists each one with its location")

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
    return checks


# Git copies this into every new clone and runs it after checkout. $1 is the previous HEAD, which is
# all zeros only on a fresh clone, so switching branches later stays silent.
HOOK = """#!/bin/sh
# Installed by setupdoctor. Remove with: setupdoctor --uninstall-hook
case "$1" in *[!0]*) exit 0 ;; esac
[ -x "{python}" ] && "{python}" -m setupdoctor --hook
exit 0
"""


def git_global(*args):
    return subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True).stdout.strip()


def clone_hook(install):
    """Add or remove the git template hook that runs setupdoctor after every `git clone`."""
    own = Path.home() / ".setupdoctor" / "git-template"
    template = Path(git_global("init.templateDir") or own).expanduser()  # reuse a template folder you already have
    hook = template / "hooks" / "post-checkout"
    ours = "setupdoctor" in read(hook)
    if not install:
        if ours:
            hook.unlink()
        if template == own:
            git_global("--unset", "init.templateDir")
        return 0, "Removed. New clones no longer run setupdoctor." if ours else "No setupdoctor hook was installed."
    if hook.exists() and not ours:
        return 1, f"{hook} already exists and isn't setupdoctor's, so it was left alone."
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK.format(python=Path(sys.executable).as_posix()), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    git_global("init.templateDir", template.as_posix())
    return 0, ("Installed. Every `git clone` from now on runs setupdoctor in the new folder.\n"
               "Repos you already cloned are not affected. Undo with: setupdoctor --uninstall-hook")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="setupdoctor", description=__doc__.split(": ", 1)[1])
    ap.add_argument("path", nargs="?", default=".", help="repo to check (default: current folder)")
    ap.add_argument("--ci", action="store_true",
                    help="only check that every env var the code reads is in .env.example (for pipelines)")
    ap.add_argument("--install-hook", action="store_true",
                    help="run setupdoctor automatically after every git clone (changes your global git config)")
    ap.add_argument("--uninstall-hook", action="store_true", help="undo --install-hook")
    ap.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)  # set by the git hook: quiet if nothing to check
    args = ap.parse_args(argv)
    if args.install_hook or args.uninstall_hook:
        if not on_path("git"):
            ap.error("git was not found on PATH")
        code, message = clone_hook(install=args.install_hook)
        print(message)
        return code
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
    if not checks and args.hook:
        return 0
    if not checks:
        print("setupdoctor: " + ("the code reads no environment variables that need documenting" if args.ci
                                else "nothing to check here (no Node or Python requirements found)"))
        return 0
    print(f"setupdoctor: {len(checks)} check{'s' * (len(checks) != 1)}\n")
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
