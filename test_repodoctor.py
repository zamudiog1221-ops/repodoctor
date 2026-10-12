"""Run with: python test_repodoctor.py"""
import contextlib
import io
import tempfile
import textwrap
from pathlib import Path

import repodoctor as rd
from repodoctor import FAIL, OK, WARN


def repo(files):
    root = Path(tempfile.mkdtemp(prefix="repodoctor-test-"))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


def machine(node=None, python=(3, 13, 0), tools=(), open_ports=()):
    """Fake the machine so results don't depend on what's installed here."""
    rd.run_version = lambda *cmds: node if cmds[0] == "node" else python
    rd.on_path = lambda cmd: cmd in tools
    rd.port_open = lambda port: port in open_ports


def has(checks, status, text):
    return any(s == status and text in msg for s, msg, _ in checks)


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
    assert any(fix == "pnpm install" for _, _, fix in checks)

    machine(node=(20, 1, 0), tools=("pnpm",))
    (root / "node_modules").mkdir()
    checks = rd.diagnose(root, env={})
    assert [s for s, _, _ in checks] == [OK, OK], checks


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
    assert not any(s == FAIL and v in msg for s, msg, _ in checks
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
    assert sorted(msg.split()[0] for s, msg, _ in checks if s == FAIL) == [
        "CF_EMAIL", "DATABASE_URL", "JWT_SECRET", "S3_BUCKET", "SESSION_SECRET"]
    assert len(checks) == 5  # ci mode reports nothing about this machine

    (root / ".env.example").write_text(
        "STRIPE_KEY=\nFLAG=\nDATABASE_URL=\nSESSION_SECRET=\nJWT_SECRET=\nS3_BUCKET=\nCF_EMAIL=\n")
    assert [s for s, _, _ in rd.diagnose(root, ci=True, env={})] == [OK]


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
    assert [s for s, _, _ in rd.diagnose(root, env={"VIRTUAL_ENV": "x"})] == [OK]


def test_cli_output():
    machine(node=(18, 0, 0))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rd.main([str(repo({".nvmrc": "20", "package.json": "{}"}))])
    assert code == 1 and "\033" not in out.getvalue() and "1 failed" in out.getvalue(), out.getvalue()
    with contextlib.redirect_stdout(io.StringIO()):
        assert rd.main([str(repo({"notes.txt": "hi"}))]) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
