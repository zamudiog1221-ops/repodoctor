#!/usr/bin/env python3
"""setupdoctor: read a repo's setup requirements from its code and check them against this machine."""
import os
import sys

# `python -m setupdoctor` puts the current folder first on the import path. That folder is a repo we
# don't trust, so drop it before importing anything a file in it could shadow (ast.py, json.py, ...).
if sys.path and os.path.abspath(sys.path[0] or ".") == os.getcwd() != os.path.dirname(os.path.abspath(__file__)):
    del sys.path[0]

import argparse  # noqa: E402
import ast  # noqa: E402
import functools  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402

OK, FAIL, WARN = "ok", "fail", "warn"
MARK = {OK: ("✓", "32"), FAIL: ("✗", "31"), WARN: ("!", "33")}

SKIP_DIRS = {"node_modules", "venv", "env", "dist", "build", "out", "target", "vendor", "coverage", "obj",
             "__pycache__", "site-packages", "test", "tests", "__tests__", "e2e", "docs", "examples",
             "fixtures", "samples", "sample", "demo", "demos", "benchmarks", "third_party"}
# Also skipped: folders named like api-e2e or integration_tests, and template folders such as
# cookiecutter's {{cookiecutter.project_slug}}, whose contents aren't a project until rendered.
SKIP_DIR_LIKE = re.compile(r"(?i)(^|[-_.])(e2e|tests?|specs?|examples?|fixtures?|benchmarks?|demos?|samples?)($|[-_.])|\{\{|\{%")
SKIP_FILE = re.compile(r"(^test_|^conftest\.|_test\.|_spec\.|\.test\.|\.spec\.|\.min\.|"
                       r"^(playwright|jest|vitest|cypress|karma)\.config\.|^wdio\.)")
# Settings that only apply to a deployed copy; what they read is never needed to run the project locally.
DEPLOY_FILE = re.compile(r"(?i)(^|[/_.\-])(prod|production|staging|deploy|deployment)([/_.\-]|$)")
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Variables the shell, the runtime or the host platform sets; nobody puts these in .env.
AMBIENT = {"NODE_ENV", "PATH", "HOME", "USER", "PWD", "SHELL", "TERM", "LANG", "TZ", "EDITOR", "CI",
           "TMPDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOSTNAME", "HOST", "PORT",
           "DEBUG", "PYTHONPATH", "VIRTUAL_ENV", "NO_COLOR", "FORCE_COLOR", "VERCEL", "NETLIFY",
           "MODE", "DEV", "PROD", "SSR", "BASE_URL", "PROGRAMFILES", "SYSTEMROOT", "WINDIR", "COMSPEC", "OS",
           "RAILS_ENV", "RACK_ENV", "APP_ENV", "GOPATH", "GOROOT", "JAVA_HOME", "GEM_HOME", "RUST_LOG",
           "RUST_BACKTRACE", "OUT_DIR", "ASPNETCORE_ENVIRONMENT"}
AMBIENT_PREFIX = ("npm_", "GITHUB_", "VERCEL_", "XDG_", "RUNNER_", "CARGO_", "BUNDLE_", "DOTNET_")

# A read is "hard" when the code breaks without the variable: an unconditional os.environ["X"],
# TypeScript's process.env.X! assertion, an `if (!process.env.X) throw`, or a required field in a
# settings schema. It is "optional" when the code copes: a fallback, a ternary or && test, or an
# assignment. Everything else (os.getenv("X"), a bare process.env.X, any read inside an if or try) is
# treated as a setting the app can run without, because on real repos that is what it nearly always is.
JS_ENV = re.compile(r"""(?:process\.env|import\.meta\.env)(?:\.([A-Z][A-Z0-9_]*)|\[\s*['"](\w+)['"]\s*\])"""
                    r"""(!(?!=))?(\s*(?:\|\||\?|&&|=(?!=)))?""")
JS_DESTRUCT = re.compile(r"\{([^}]*)\}\s*=\s*process\.env\b(?!\s*[.\[])")
JS_GUARD = re.compile(r"if\s*\(\s*!\s*process\.env\.([A-Z][A-Z0-9_]*)\s*\)\s*\{?\s*(?:throw\b|process\.exit)")
# Settings schemas: zod (and t3-env, which wraps it), envalid and Joi.
SCHEMA_LIB = re.compile(r"""['"](zod|@t3-oss/env-[\w-]+|envalid|joi|@hapi/joi)['"]""")
SCHEMA_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*:\s*(z\b|[Jj]oi\b|(?:str|num|bool|url|email|host|port|json)\()")
# Other languages: reads are only ever used to check that a variable is documented, never to fail a
# machine check. Group 1 is the name; a second group, when it matches, means the read has a fallback.
OTHER_ENV = {ext: [re.compile(rx) for rx in patterns] for ext, patterns in {
    ".rb": [r"""ENV\[['"]([A-Z][A-Z0-9_]*)['"]\](\s*\|\|)?""", r"""ENV\.fetch\(\s*['"]([A-Z][A-Z0-9_]*)['"]\s*(,|\)\s*\{)?"""],
    ".go": [r"""os\.(?:Getenv|LookupEnv)\("([A-Z][A-Z0-9_]*)"\)"""],
    ".php": [r"""(?<![\w>])(?:env|getenv)\(\s*['"]([A-Z][A-Z0-9_]*)['"]\s*(,)?""", r"""\$_ENV\[['"]([A-Z][A-Z0-9_]*)['"]\]"""],
    ".java": [r"""System\.getenv\("([A-Z][A-Z0-9_]*)"\)"""],
    ".kt": [r"""System\.getenv\("([A-Z][A-Z0-9_]*)"\)"""],
    ".rs": [r"""env::var(?:_os)?\("([A-Z][A-Z0-9_]*)"\)""", r"""\benv!\("([A-Z][A-Z0-9_]*)"\)"""],
    ".cs": [r"""Environment\.GetEnvironmentVariable\("([A-Z][A-Z0-9_]*)"\)"""],
}.items()}

ENV_FILES = [".env", ".env.local", ".env.development", ".env.development.local"]
# Example file -> the file you are meant to copy it to.
EXAMPLE_TARGET = {".env.example": ".env", ".env.sample": ".env", ".env.template": ".env", ".env.dist": ".env",
                  ".env.local.example": ".env.local", "example.env": ".env", "env.example": ".env"}
ENV_EXAMPLES = list(EXAMPLE_TARGET)

NODE_LOCKS = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")]
NODE_OWN_LOCKS = [name for name, _ in NODE_LOCKS] + ["package-lock.json"]
PY_LOCKS = [("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile", "pipenv")]
PY_FILES = {"requirements.txt", "Pipfile", "setup.py", "uv.lock", "poetry.lock", ".python-version"}
PY_PROJECT = re.compile(r"(?m)^\[(project|tool\.poetry)\]")
NODE_PMS = {"npm", "yarn", "pnpm", "bun"}
INSTALL = {"pnpm": ["npm", "install", "-g", "pnpm"], "yarn": ["npm", "install", "-g", "yarn"],
           "bun": ["npm", "install", "-g", "bun"], "uv": ["pip", "install", "uv"],
           "poetry": ["pipx", "install", "poetry"], "pipenv": ["pip", "install", "pipenv"]}
# Fixes setupdoctor can run are argument lists built from the names above, never from text in the repo.
COPY = "copy"  # built-in action: [COPY, source, destination], done in Python
IN = "in"      # wrapper: [IN, folder, *command] runs the command inside that sub-project folder
VENV_PYTHON = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
MAX_SUBPROJECTS = 12
SCAN_SECONDS = 8  # how long the interactive check may spend reading source files

SERVICES = {5432: "Postgres", 6379: "Redis", 27017: "MongoDB", 3306: "MySQL"}
# ponytail: pattern match, not a YAML parser; misses ports written as ${VAR:-5432}:5432. Parse YAML if that bites.
COMPOSE_PORT = re.compile(r"""(?m)^\s*-\s*["']?(?:[\d.]+:)?(\d+):(\d+)(?:/tcp)?["']?\s*$""")

# Names asdf and mise use in .tool-versions / mise.toml, and the program each one provides.
TOOL_CMDS = {"nodejs": "node", "node": "node", "python": "python", "ruby": "ruby", "golang": "go", "go": "go",
             "java": "java", "rust": "rustc", "php": "php", "terraform": "terraform", "deno": "deno", "bun": "bun",
             "dotnet": "dotnet", "dotnet-core": "dotnet"}
TOOL_LABEL = {"node": "Node", "python": "Python", "ruby": "Ruby", "go": "Go", "java": "Java", "rustc": "Rust",
              "php": "PHP", "terraform": "Terraform", "deno": "Deno", "bun": "Bun", "dotnet": ".NET"}
VERSION_ARGS = {"go": ["version"], "java": ["-version"]}


def skip_dir(name):
    return name in SKIP_DIRS or name.startswith(".") or bool(SKIP_DIR_LIKE.search(name))


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def first_line(path):
    return (read(path).strip().splitlines() or [""])[0].strip()


def load_json(path):
    """A JSON file as a dict; {} if it is missing, broken or not an object."""
    try:
        data = json.loads(read(path) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def version(text):
    m = re.search(r"\d+(?:\.\d+)*", text or "")
    return tuple(int(p) for p in m.group().split(".")) if m else None


def newest(spec):
    """The highest version a spec mentions: for "^7.4 || ^8.0" the one worth installing is 8.0."""
    return max(v for v in map(version, spec.split("||")) if v)


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


def which(name):
    """Full path of a program on PATH. Unlike shutil.which on Windows, it never looks in the current
    folder: that folder is a repo we don't trust, and a node.cmd planted there must not get run."""
    exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if os.name == "nt" else [""]
    here = os.path.normcase(os.getcwd())
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(folder) or os.path.normcase(os.path.abspath(folder)) == here:
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def run_version(*cmds, cwd=None):
    """Version tuple of the first of `cmds` that runs, or None. It runs in the project folder so that
    version managers that switch by folder (pyenv, rbenv, asdf, mise) report what you'd really get."""
    for cmd in cmds:
        exe = which(cmd)
        if not exe:
            continue
        try:  # GOTOOLCHAIN=local: asking Go for its version must not make it download another one
            out = subprocess.run([exe, *VERSION_ARGS.get(cmd, ["--version"])], capture_output=True, text=True,
                                 timeout=15, cwd=cwd, env=dict(os.environ, GOTOOLCHAIN="local"))
        except (OSError, subprocess.SubprocessError):
            continue
        found = version(out.stdout + out.stderr) if out.returncode == 0 else None
        if found:
            return found
    return None


def on_path(cmd):
    return which(cmd) is not None


@functools.lru_cache(maxsize=None)
def npm_prefix():
    npm = which("npm")
    try:
        return subprocess.run([npm, "prefix", "-g"], capture_output=True, text=True, timeout=30).stdout.strip() if npm else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def npm_global(name):
    """Full path of a program in npm's global folder, or None. That folder is often missing from PATH
    (a Windows machine set up without admin rights, for one), so `npm install -g yarn` can succeed and
    `yarn` still not be found."""
    prefix = npm_prefix()
    candidate = os.path.join(prefix, name + ".cmd") if os.name == "nt" else os.path.join(prefix, "bin", name)
    return candidate if prefix and os.path.isfile(candidate) else None


def port_open(port):
    try:
        socket.create_connection(("localhost", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait_for_port(port, seconds):
    """port_open, retried for up to `seconds`: a container that just started takes a moment to listen."""
    deadline = time.monotonic() + seconds
    while not port_open(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    return True


def docker_running():
    """True if the Docker engine answers. Docker being installed doesn't mean it has been started."""
    try:
        return subprocess.run([which("docker"), "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, TypeError, subprocess.SubprocessError):
        return False


def js_env_uses(text):
    """Yield (name, line number, optional, hard) for each env var a JS/TS file reads."""
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
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
    for m in JS_GUARD.finditer(text):
        yield m.group(1), text.count("\n", 0, m.start()) + 1, False, True
    if SCHEMA_LIB.search(text) and re.search(r"process\.env|import\.meta\.env|createEnv|cleanEnv", text):
        for i, line in enumerate(lines):
            m = SCHEMA_KEY.match(line)
            if not m or len(line) > 2000:
                continue
            entry = line  # a schema entry can run over several lines; it ends at the trailing comma
            for more in lines[i + 1:i + 8]:
                if entry.rstrip().endswith((",", "}")):
                    break
                entry += more
            kind = m.group(2)
            if kind == "z":
                required = not re.search(r"\.(optional|default|nullish|catch)\(", entry)
            elif kind.lower() == "joi":
                required = ".required()" in entry
            else:  # envalid: str(), url(), ... are required unless given a default or devDefault
                required = "efault" not in entry
            if required:
                yield m.group(1), i + 1, False, True


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

    def name_of(node):
        return getattr(node, "id", None) or getattr(node, "attr", None)

    # django-environ's env("X") and python-decouple's config("X") raise when X is unset and has no default.
    django_environ = bool(re.search(r"(?m)^\s*(import environ\b|from environ\b)", text))
    decouple = bool(re.search(r"(?m)^\s*(import decouple\b|from decouple\b)", text))
    # environ.Env(DEBUG=(bool, False)) declares defaults up front
    declared_defaults = {kw.arg for node in ast.walk(tree) if isinstance(node, ast.Call) and name_of(node.func) == "Env"
                         for kw in node.keywords if kw.arg} if django_environ else set()

    uses, stack = [], [(tree, False, False)]
    while stack:
        node, guarded, in_or = stack.pop()
        if isinstance(node, ast.Subscript) and is_environ(node.value) and text_of(node.slice) \
                and isinstance(node.ctx, ast.Load):
            uses.append((text_of(node.slice), node.lineno, False, not guarded))
        elif isinstance(node, ast.Call) and node.args and text_of(node.args[0]):
            f, name = node.func, text_of(node.args[0])
            has_default = len(node.args) > 1 or any(kw.arg == "default" for kw in node.keywords) or in_or
            if (isinstance(f, ast.Name) and f.id == "getenv") or (isinstance(f, ast.Attribute) and (
                    f.attr == "getenv" or (f.attr == "get" and is_environ(f.value)))):
                uses.append((name, node.lineno, has_default or bool(node.keywords), False))
            elif (django_environ and (name_of(f) == "env" or name_of(getattr(f, "value", None)) == "env")) \
                    or (decouple and isinstance(f, ast.Name) and f.id == "config"):
                uses.append((name, node.lineno, has_default or name in declared_defaults, not guarded))
        elif isinstance(node, ast.ClassDef) and any(name_of(base) == "BaseSettings" for base in node.bases):
            # pydantic-settings: a field with no default must come from the environment
            prefix = next((kw.value.value for item in node.body for kw in ast.walk(item)
                           if isinstance(kw, ast.keyword) and kw.arg == "env_prefix" and text_of(kw.value) is not None), "")
            for item in node.body:
                if isinstance(item, ast.ClassDef) and item.name == "Config":
                    prefix = next((text_of(a.value) for a in item.body if isinstance(a, ast.Assign)
                                   and name_of(a.targets[0]) == "env_prefix" and text_of(a.value) is not None), prefix)
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and item.value is None and isinstance(item.target, ast.Name) \
                        and not item.target.id.startswith("_"):
                    kind = ast.unparse(item.annotation)
                    if not re.search(r"None|Optional|ClassVar", kind):
                        uses.append(((prefix + item.target.id).upper(), item.lineno, False, True))
        guarded = guarded or isinstance(node, (ast.If, ast.IfExp, ast.Try))
        is_or = isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        stack.extend((child, guarded, is_or) for child in ast.iter_child_nodes(node))
    # os.environ.setdefault("X", ...) anywhere in the file gives X a default
    defaulted = {text_of(node.args[0]) for node in ast.walk(tree) if isinstance(node, ast.Call) and node.args
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault" and is_environ(node.func.value)}
    return sorted(((name, line, optional or name in defaulted, hard) for name, line, optional, hard in uses),
                  key=lambda use: use[1])


def other_env_uses(text, patterns):
    """Yield (name, line number, optional, hard) for env reads in Ruby, Go, PHP, Java, Kotlin, Rust or C#."""
    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000 or line.lstrip().startswith(("#", "//", "*")):
            continue
        for rx in patterns:
            for m in rx.finditer(line):
                yield m.group(1), n, bool(m.lastindex and m.lastindex > 1), False


def scan_env(root, exclude=(), budget=None):
    """Map each env var the source reads without a fallback to ('file:line' of first use, hard).
    `exclude` holds folders that are projects of their own and get scanned separately. `budget` is
    {"until": time, "hit": False}: reading thousands of files can take a minute on a machine whose
    virus scanner checks each one, and a check that runs after every clone must not do that."""
    reads, exclude = {}, {str(p) for p in exclude}
    for dirpath, dirs, files in os.walk(root):
        if budget and time.monotonic() > budget["until"]:
            budget["hit"] = True
            break
        dirs[:] = sorted(d for d in dirs if not skip_dir(d) and os.path.join(dirpath, d) not in exclude)
        for name in sorted(files):
            ext = os.path.splitext(name)[1]
            if not (ext in JS_EXT or ext == ".py" or ext in OTHER_ENV) or SKIP_FILE.search(name):
                continue
            path = Path(dirpath, name)
            try:
                if path.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            text = read(path)
            if "env" not in text.lower() and "BaseSettings" not in text and "config(" not in text:
                continue
            where = path.relative_to(root).as_posix()
            uses = (js_env_uses(text) if ext in JS_EXT else py_env_uses(text) if ext == ".py"
                    else other_env_uses(text, OTHER_ENV[ext]))
            for var, n, optional, hard in uses:
                if optional or var in AMBIENT or var.startswith(AMBIENT_PREFIX):
                    continue
                hard = hard and not DEPLOY_FILE.search(where)
                if var not in reads or (hard and not reads[var][1]):
                    reads[var] = (f"{where}:{n}", hard)
    return reads


def env_keys(root, names, commented=False):
    """Variable names defined in the given env files; `commented` also counts '# KEY=' lines."""
    rx = re.compile(r"^\s*%s(?:export\s+)?(\w+)\s*=" % (r"#?\s*" if commented else ""), re.M)
    return {key for name in names for key in rx.findall(read(root / name))}


def pinned_tools(root):
    """{program: (file, version)} from .tool-versions and mise.toml, the files asdf and mise read."""
    pins = {}
    for line in read(root / ".tool-versions").splitlines():
        parts = line.split("#")[0].split()
        if len(parts) >= 2 and parts[0] in TOOL_CMDS:
            pins.setdefault(TOOL_CMDS[parts[0]], (".tool-versions", parts[1]))
    for name in ("mise.toml", ".mise.toml"):
        section = re.search(r"(?ms)^\[tools\]\s*$(.*?)(?=^\[|\Z)", read(root / name))
        for tool, pinned in re.findall(r"""(?m)^\s*([\w-]+)\s*=\s*\[?\s*["']([^"']+)""", section.group(1) if section else ""):
            if tool in TOOL_CMDS:
                pins.setdefault(TOOL_CMDS[tool], (name, pinned))
    return pins


def check_version(label, found, spec, source, n, fix):
    shown_spec = re.sub(r"^[A-Za-z][A-Za-z-]*(?=\d)", "", spec.strip())  # "python-3.12.4" and "v20.11.0" read better bare
    need = f"{label} {shown_spec} required ({source})"
    if not found:
        return FAIL, f"{need}, but no working {label} was found", fix
    shown = ".".join(map(str, found))
    if satisfies(found, spec, n):
        return OK, f"{need}, found {shown}", None
    return FAIL, f"{need}, found {shown}", fix


# ---- The simpler languages: a version, the tools the project needs, and sometimes a dependency folder.

def php_specs(root):
    spec = load_json(root / "composer.json").get("require")
    spec = spec.get("php", "") if isinstance(spec, dict) else ""
    return [("composer.json", re.sub(r"\s*\|\|?\s*", " || ", str(spec)))]  # composer writes "or" as | or ||


def ruby_specs(root):
    gemfile = re.search(r"""(?m)^\s*ruby\s+["']([^"']+)["']""", read(root / "Gemfile"))
    return [(".ruby-version", first_line(root / ".ruby-version")),
            ("Gemfile", gemfile.group(1).replace("~>", "~") if gemfile else "")]


def go_specs(root):
    m = re.search(r"(?m)^go\s+(\d+(?:\.\d+)*)", read(root / "go.mod"))
    return [("go.mod", ">=" + m.group(1) if m else "")]


def rust_specs(root):
    pinned = re.search(r"""channel\s*=\s*["'](\d+\.\d+[\d.]*)""", read(root / "rust-toolchain.toml") + read(root / "rust-toolchain"))
    plain = first_line(root / "rust-toolchain")
    oldest = re.search(r"""(?m)^rust-version\s*=\s*["']([\d.]+)""", read(root / "Cargo.toml"))
    return [("rust-toolchain", pinned.group(1) if pinned else plain if re.match(r"\d+\.\d+", plain) else ""),
            ("Cargo.toml", ">=" + oldest.group(1) if oldest else "")]


def java_number(text):
    """Java versions as people write them: '1.8', '1_8' and 'VERSION_1_8' all mean 8."""
    text = (text or "").replace("_", ".")
    return text[2:] if text.startswith("1.") else text


def java_specs(root):
    pom, gradle = read(root / "pom.xml"), read(root / "build.gradle") + read(root / "build.gradle.kts")
    sdkman = re.search(r"(?m)^java=(\S+)", read(root / ".sdkmanrc"))
    maven = re.search(r"<(?:maven\.compiler\.(?:release|source|target)|java\.version)>\s*([\d.]+)\s*<", pom)
    build = re.search(r"(?:JavaLanguageVersion\.of|jvmToolchain)\((\d+)\)", gradle) or re.search(
        r"""(?:source|target)Compatibility\s*=?\s*(?:JavaVersion\.VERSION_)?['"]?([\d_.]+)""", gradle)
    # A newer JDK can build for an older release, so the build files set a minimum, not an exact version.
    return [(".java-version", java_number(first_line(root / ".java-version"))),
            (".sdkmanrc", sdkman.group(1) if sdkman else ""),
            ("pom.xml", ">=" + java_number(maven.group(1)) if maven else ""),
            ("build.gradle", ">=" + java_number(build.group(1)) if build else "")]


def java_tools(root):
    tools = []
    if (root / "pom.xml").exists() and not (root / "mvnw").exists():
        tools.append(("mvn", "pom.xml, and the repo has no mvnw wrapper", "install Maven (https://maven.apache.org)"))
    if ((root / "build.gradle").exists() or (root / "build.gradle.kts").exists()) and not (root / "gradlew").exists():
        tools.append(("gradle", "build.gradle, and the repo has no gradlew wrapper", "install Gradle (https://gradle.org/install)"))
    return tools


def dotnet_specs(root):
    sdk = load_json(root / "global.json").get("sdk")
    sdk = sdk if isinstance(sdk, dict) else {}
    if sdk.get("version"):
        forward = str(sdk.get("rollForward", "")).lower()  # "major"/"latestMajor" accept any newer SDK
        return [("global.json", (">=" if "major" in forward else "") + str(sdk["version"]))]
    targets = [m for path in sorted(root.glob("*.*proj")) for m in re.findall(r"\bnet(\d+\.\d+)\b", read(path))]
    return [("project file", ">=" + max(targets, key=version) if targets else "")]


LANGS = [
    {"name": "PHP", "files": ("composer.json",), "cmd": "php", "pin": 2, "specs": php_specs,
     "get": "install PHP{v} (https://www.php.net/downloads)",
     "tools": lambda root: [("composer", "composer.json", "install Composer (https://getcomposer.org)")],
     "deps": ("vendor/autoload.php", ["composer", "install"])},  # composer always writes this file
    {"name": "Ruby", "files": ("Gemfile",), "cmd": "ruby", "pin": 2, "specs": ruby_specs,
     "get": "install Ruby{v} (for example: rbenv install{v})",
     "tools": lambda root: [("bundle", "Gemfile", "gem install bundler")]},
    # Go 1.21 and later fetch a newer toolchain themselves when go.mod asks for one.
    {"name": "Go", "files": ("go.mod",), "cmd": "go", "pin": 2, "specs": go_specs, "self_updating_from": (1, 21),
     "get": "install Go{v} (https://go.dev/dl)"},
    # rustup installs whatever toolchain the repo pins the first time cargo runs.
    {"name": "Rust", "files": ("Cargo.toml",), "cmd": "rustc", "pin": 2, "specs": rust_specs, "managed_by": "rustup",
     "get": "install Rust with rustup (https://rustup.rs)",
     "tools": lambda root: [("cargo", "Cargo.toml", "install Rust with rustup (https://rustup.rs)")]},
    {"name": "Java", "files": ("pom.xml", "build.gradle", "build.gradle.kts"), "cmd": "java", "pin": 1,
     "specs": java_specs, "get": "install JDK{v} (https://adoptium.net)", "tools": java_tools},
    # global.json makes `dotnet --version` fail inside the repo when its SDK is missing, so ask from elsewhere.
    {"name": ".NET", "files": ("global.json",), "suffixes": (".sln", ".csproj", ".fsproj"), "cmd": "dotnet", "pin": 1,
     "specs": dotnet_specs, "neutral_cwd": True, "get": "install the .NET{v} SDK (https://dotnet.microsoft.com/download)"},
]


def langs_in(folder):
    """Names of the languages that have a project file directly in this folder."""
    try:
        names = {entry.name for entry in os.scandir(folder) if entry.is_file()}
    except OSError:
        return set()
    found = {lang["name"] for lang in LANGS
             if names & set(lang["files"]) or any(name.endswith(lang.get("suffixes", ())) for name in names)}
    if "package.json" in names:
        found.add("Node")
    if names & PY_FILES or ("pyproject.toml" in names and PY_PROJECT.search(read(Path(folder, "pyproject.toml")))):
        found.add("Python")
    return found


def has_example(folder):
    return any((folder / name).exists() for name in ENV_EXAMPLES)


def find_subprojects(root):
    """Sub-folders, up to two levels down, that hold a project or an env example file of their own."""
    def folders(parent):
        try:
            return sorted(p for p in parent.iterdir() if p.is_dir() and not skip_dir(p.name))
        except OSError:
            return []

    def counts(folder):
        return bool(langs_in(folder)) or has_example(folder)

    found = []
    for one in folders(root):
        found += [one] if counts(one) else []
        found += [two for two in folders(one) if counts(two)]
    return found


def check_node(root, pins, add):
    pkg = load_json(root / "package.json")
    node = run_version("node", cwd=root)
    engines = pkg.get("engines")
    specs = [(".nvmrc", first_line(root / ".nvmrc")), (".node-version", first_line(root / ".node-version")),
             pins.pop("node", ("", "")),
             ("package.json engines", str(engines.get("node", "")) if isinstance(engines, dict) else "")]
    node_ok = bool(node)
    for source, spec in specs:
        if version(spec):
            check = check_version("Node", node, spec, source, 1, f"nvm install {newest(spec)[0]}")
            node_ok = check[0] == OK
            add(*check)
            break
    else:
        if not node:
            add(FAIL, "Node is needed (package.json) but was not found on PATH",
                "install Node from https://nodejs.org")
    pm = str(pkg.get("packageManager") or "").split("@")[0]
    if pm not in NODE_PMS:  # anything else is text from the repo, not a program we know
        pm = next((p for f, p in NODE_LOCKS if (root / f).exists()), "npm")
    have_pm, get_pm = on_path(pm), None
    if pm != "npm" and not have_pm and npm_global(pm):
        have_pm = True  # setupdoctor can still run it by its full path
        folder = os.path.dirname(npm_global(pm))
        add(WARN, f"{pm} is installed in {folder}, but that folder is not on your PATH, so typing `{pm}` won't work",
            f"add {folder} to your PATH")
    elif pm != "npm" and not have_pm:
        get_pm = INSTALL[pm] if on_path("npm") else None
        add(FAIL, f"{pm} is this repo's package manager but is not installed", " ".join(INSTALL[pm]),
            get_pm and [get_pm])
    if pkg.get("dependencies") or pkg.get("devDependencies"):
        if (root / "node_modules").is_dir():
            add(OK, "Node dependencies installed (node_modules exists)", None)
        else:  # installing with the wrong Node can leave a broken node_modules, so that comes first
            add(FAIL, "Node dependencies not installed (no node_modules folder)",
                f"{pm} install" + ("" if node_ok else " (after fixing the Node version)"),
                [[pm, "install"]] if node_ok and (have_pm or get_pm) else None)


def check_python(root, pins, env, add):
    pyproject = read(root / "pyproject.toml")
    pm = next((p for f, p in PY_LOCKS if (root / f).exists()), None)
    py_spec = re.search(r"""(?m)^(?:requires-python|python)\s*=\s*["']([^"']+)""", pyproject)
    specs = [(".python-version", first_line(root / ".python-version")),
             ("runtime.txt", first_line(root / "runtime.txt")),
             pins.pop("python", ("", "")),
             ("pyproject.toml", py_spec.group(1) if py_spec else "")]
    py_ok = True
    if pm != "uv":  # uv downloads the right Python by itself
        python = run_version("python", "python3", cwd=root) or tuple(sys.version_info[:3])
        for source, spec in specs:
            if version(spec):
                want = ".".join(map(str, newest(spec)[:2]))
                check = check_version("Python", python, spec, source, 2,
                                      f"install Python {want} (for example: pyenv install {want})")
                py_ok = check[0] == OK
                add(*check)
                break
    have_pm, get_pm = not pm or on_path(pm), None
    if not have_pm:
        get_pm = INSTALL[pm] if on_path(INSTALL[pm][0]) else None
        add(FAIL, f"{pm} is this repo's package manager but is not installed", " ".join(INSTALL[pm]),
            get_pm and [get_pm])
    # poetry and pipenv keep virtualenvs outside the repo, so there's nothing to look for.
    if pm in (None, "uv") and not env.get("VIRTUAL_ENV") \
            and not any((root / d).is_dir() for d in (".venv", "venv", "env")):
        if pm == "uv":
            fix, run = "uv sync", [["uv", "sync"]] if have_pm or get_pm else None
        else:
            target = ["-r", "requirements.txt"] if (root / "requirements.txt").exists() else ["-e", "."]
            fix = "python -m venv .venv, activate it, then pip install " + " ".join(target)
            run = [[sys.executable, "-m", "venv", ".venv"],
                   [VENV_PYTHON, "-m", "pip", "install", *target]] if py_ok else None
        add(WARN, "No virtual environment found (.venv or venv), so dependencies are probably not installed",
            fix, run)


def check_simple(lang, root, pins, add):
    name, pin = lang["name"], pins.pop(lang["cmd"], ("", ""))
    marker = next((f for f in lang["files"] if (root / f).exists()), "its project file")
    specs = [(source, spec) for source, spec in lang["specs"](root) + [pin] if version(spec)]
    managed = lang.get("managed_by")
    if managed and on_path(managed):  # asking the compiler for its version here would make rustup download one
        if specs:
            add(OK, f"{name} {specs[0][1]} required ({specs[0][0]}); {managed} installs it the first time you build")
        found = ok = True
    else:
        found = run_version(lang["cmd"], cwd=tempfile.gettempdir() if lang.get("neutral_cwd") else root)
        if name == "Java" and found and found[0] == 1:
            found = found[1:]  # Java 8 calls itself 1.8
        ok = bool(found)
        if specs:
            source, spec = specs[0]
            want = ".".join(map(str, newest(spec)[:2]))
            newer = lang.get("self_updating_from")
            if found and newer and found >= newer and not satisfies(found, spec, lang["pin"]):
                add(OK, f"{name} {spec} required ({source}), found {'.'.join(map(str, found))}, "
                        f"which downloads the newer toolchain itself")
            else:
                check = check_version(name, found, spec, source, lang["pin"], lang["get"].format(v=" " + want))
                ok = check[0] == OK
                add(*check)
        elif not found:
            add(FAIL, f"{name} is needed ({marker}) but was not found on PATH", lang["get"].format(v=""))
    missing_tool = False
    for tool, why, how in lang.get("tools", lambda root: [])(root):
        if found and not on_path(tool):
            missing_tool = True
            add(FAIL, f"{tool} is needed ({why}) but is not installed", how)
    if lang.get("deps"):
        proof, install = lang["deps"]
        if (root / proof).exists():
            add(OK, f"{name} dependencies installed ({proof} exists)")
        else:
            add(FAIL, f"{name} dependencies not installed (no {proof})",
                " ".join(install) + ("" if ok else f" (after fixing the {name} version)"),
                [install] if ok and not missing_tool else None)


def diagnose_dir(root, ci, env, port_wait, langs, exclude=(), parent=None, budget=None):
    """The checks for one project folder. `parent` is the repo root when this is a sub-project: its
    env files count too, because a monorepo often keeps one .env at the top."""
    checks = []

    def add(status, message, fix=None, run=None):
        checks.append((status, message, fix, run))

    shared = [parent] if parent else []
    reads = scan_env(root, exclude, budget)
    example = next((n for n in ENV_EXAMPLES if (root / n).exists()), None)
    documented = set().union(*(env_keys(folder, ENV_EXAMPLES, commented=True) for folder in [root] + shared))
    undocumented = sorted(v for v in reads if v not in documented)

    if ci:
        for var in undocumented:
            add(FAIL, f"{var} is read at {reads[var][0]} but is not in {example or '.env.example'}",
                f"add {var}= to {example or '.env.example'}")
        if reads and not undocumented:
            add(OK, f"All {len(reads)} environment variables are documented in {example or '.env.example'}", None)
        return checks

    pins = pinned_tools(root)
    if "Node" in langs:
        check_node(root, pins, add)
    if "Python" in langs:
        check_python(root, pins, env, add)
    for lang in LANGS:
        if lang["name"] in langs:
            check_simple(lang, root, pins, add)
    for cmd, (source, spec) in pins.items():  # tools pinned for asdf/mise that no project file accounts for
        if version(spec):
            add(*check_version(TOOL_LABEL[cmd], run_version(cmd, cwd=root), spec, source, 2,
                               f"install {TOOL_LABEL[cmd]} {spec}" + (" (mise install)" if on_path("mise") else
                                                                    " (asdf install)" if on_path("asdf") else "")))

    set_keys = set().union(*(env_keys(folder, ENV_FILES) for folder in [root] + shared))
    required = [v for v, (_, hard) in reads.items() if hard]
    missing = sorted(v for v in required if v not in env and v not in set_keys)
    if required and not missing:
        add(OK, f"All {len(required)} required environment variables are set", None)
    # .env.example is a declared requirement even when the code reads env vars in a way the scan can't see.
    declared_unset = sorted(k for k in env_keys(root, ENV_EXAMPLES) if k not in env and k not in set_keys)
    if declared_unset and not any((root / n).exists() for n in ENV_FILES):
        target = EXAMPLE_TARGET[example]
        add(FAIL, f"No {target} file; {example} defines {len(declared_unset)} variable(s)",
            f"cp {example} {target}, then fill in the values", [[COPY, example, target]])
        missing = [v for v in missing if v not in documented]
    elif set(declared_unset) - set(missing):
        add(WARN, f"In {example} but not in your .env: " + ", ".join(sorted(set(declared_unset) - set(missing))),
            f"copy them over from {example} if you need them")
    for var in missing:
        add(FAIL, f"{var} is not set (first used in {reads[var][0]})", f"add {var}=... to .env")
    # Undocumented-but-optional variables are the maintainers' business, not the person who just
    # cloned the repo: that list is what --ci is for.

    declared = {}
    compose = next((n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
                    if (root / n).exists()), None)
    if compose:
        override = compose.replace(".", ".override.")  # Docker merges this file in automatically
        for host, container in COMPOSE_PORT.findall(read(root / compose) + "\n" + read(root / override)):
            if int(container) in SERVICES:
                declared[int(host)] = SERVICES[int(container)]
    for port, service in sorted(declared.items()):
        down = f"{service} ({compose}) is not reachable on localhost:{port}"
        if wait_for_port(port, port_wait):
            add(OK, f"{service} reachable on localhost:{port}", None)
        elif not on_path("docker"):
            add(FAIL, down, "install Docker, then: docker compose up -d")
        elif not docker_running():
            add(FAIL, down, "start Docker (open Docker Desktop), then: docker compose up -d")
        else:
            add(FAIL, down, "docker compose up -d", [["docker", "compose", "up", "-d"]])
    return checks


def diagnose(root, ci=False, env=os.environ, port_wait=0):
    """Return a list of (status, message, fix, run) for the repo at `root` and the projects inside it.
    `fix` is the text shown to the user; `run` is the list of commands setupdoctor can run to do it,
    or None when it has to be done by hand."""
    root = Path(root)
    top = langs_in(root)
    # A sub-folder in a language the top folder also uses is a member of that project (a workspace
    # package), installed from the top. A Node folder with its own lockfile is the exception. A member
    # that has its own .env.example still gets its env checks done in its own folder.
    subs = [(sub, langs) for sub in find_subprojects(root)
            for langs in [{name for name in langs_in(sub) if name not in top
                           or (name == "Node" and any((sub / lock).exists() for lock in NODE_OWN_LOCKS))}]
            if langs or has_example(sub)]
    unchecked, subs = len(subs) - MAX_SUBPROJECTS, subs[:MAX_SUBPROJECTS]
    folders = [sub for sub, _ in subs]
    # --ci has to be complete, so only the interactive check gets a time limit on reading source files
    budget = None if ci else {"until": time.monotonic() + SCAN_SECONDS, "hit": False}
    checks = diagnose_dir(root, ci, env, port_wait, top, exclude=folders, budget=budget)
    for sub, langs in subs:
        label = sub.relative_to(root).as_posix()
        for status, message, fix, run in diagnose_dir(sub, ci, env, port_wait, langs, parent=root, budget=budget,
                                                      exclude=[other for other in folders if sub in other.parents]):
            checks.append((status, f"[{label}] {message}", fix, run and [[IN, label, *cmd] for cmd in run]))
    if unchecked > 0:
        checks.append((WARN, f"{unchecked} more project folder(s) were not checked", "run setupdoctor inside them", None))
    if budget and budget["hit"]:
        checks.append((WARN, f"Stopped reading source files after {SCAN_SECONDS} seconds, so a required "
                             f"environment variable could have been missed", "setupdoctor --ci reads everything", None))
    return checks


def ask(question):
    """Ask on the terminal itself, which also works inside the git hook, where stdin is disconnected.
    Returns None when there is no terminal (CI, cron), so nothing ever waits for an answer there."""
    try:
        with open("CONIN$" if os.name == "nt" else "/dev/tty", encoding="utf-8", errors="replace") as terminal:
            print(question, end="", flush=True)
            return terminal.readline().strip().lower()
    except OSError:
        return None


def show(cmd):
    return f"({cmd[1]}) {show(cmd[2:])}" if cmd[0] == IN else " ".join(cmd)


def run_fixes(root, actions):
    """Run each action in its project folder, stopping at the first failure. True if all succeeded."""
    for action in actions:
        print(f"\n> {show(action)}", flush=True)
        where, cmd = (root / action[1], action[2:]) if action[0] == IN else (root, action)
        if cmd[0] == COPY:
            if not (where / cmd[2]).exists():  # never overwrite
                shutil.copyfile(where / cmd[1], where / cmd[2])
                print(f"created {cmd[2]}; fill in its values before you run the app")
            continue
        # the virtualenv this run just created is the only program taken from inside the repo
        exe = (str(where / cmd[0]) if cmd[0] == VENV_PYTHON else cmd[0] if os.path.isabs(cmd[0])
               else which(cmd[0]) or npm_global(cmd[0]))
        try:
            failed = not exe or subprocess.run([exe, *cmd[1:]], cwd=where).returncode != 0
        except OSError:
            failed = True
        if failed:
            print(f"\nsetupdoctor: `{show(action)}` did not succeed, so the steps after it were skipped.")
            return False
    return True


def offer_fixes(root, checks):
    """List the fixes setupdoctor can run, ask, and run them on a yes. True if anything was run."""
    actions = []
    for *_, run in checks:
        actions += [cmd for cmd in run or () if cmd not in actions]
    if not actions:
        return False
    listing = "".join(f"  {show(cmd)}\n" for cmd in actions)
    if ask(f"\nsetupdoctor can run these for you:\n{listing}Run them now? [y/N] ") not in ("y", "yes"):
        return False
    run_fixes(Path(root), actions)
    return True


def report(checks, color):
    """Print the checklist and summary; return the exit code."""
    print(f"setupdoctor: {len(checks)} check{'s' * (len(checks) != 1)}\n")
    for status, message, fix, _ in checks:
        mark, code = MARK[status]
        print(f"  \033[{code}m{mark}\033[0m {message}" if color else f"  {mark} {message}")
        if fix:
            print(f"      fix: {fix}")
    count = {s: sum(c[0] == s for c in checks) for s in (FAIL, WARN, OK)}
    print(f"\n{count[FAIL]} failed, {count[WARN]} warning(s), {count[OK]} passed")
    return 1 if count[FAIL] else 0


# Git copies this into every new clone and runs it after checkout. $1 is the previous HEAD, which is
# all zeros only on a fresh clone, so switching branches later stays silent.
HOOK = """#!/bin/sh
# Installed by setupdoctor. Remove with: setupdoctor --uninstall-hook
case "$1" in *[!0]*) exit 0 ;; esac
[ -x "{python}" ] && "{python}" "{script}" --hook
exit 0
"""
# The hook runs this file by its full path, not `-m setupdoctor`: -m would put the freshly cloned,
# untrusted repo first on Python's import path.


def git_global(*args):
    return subprocess.run([which("git"), "config", "--global", *args], capture_output=True, text=True).stdout.strip()


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
    with open(hook, "w", encoding="utf-8", newline="\n") as f:  # sh needs \n line endings, on Windows too
        f.write(HOOK.format(python=Path(sys.executable).as_posix(), script=Path(__file__).resolve().as_posix()))
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
                                else "nothing to check here (no project files it recognises)"))
        return 0
    code = report(checks, color)
    # Only when a person is watching: never in CI, and never when the output is going to a file.
    if not args.ci and sys.stdout.isatty() and offer_fixes(args.path, checks):
        print()
        started = any("docker" in cmd for *_, run in checks for cmd in run or ())
        code = report(diagnose(args.path, port_wait=20 if started else 0), color)
    return code


if __name__ == "__main__":
    sys.exit(main())
