# repodoctor

`brew doctor` for any repo. It reads what a project needs from its code and config files, checks your machine, and prints the fix for each gap. It never reads the README.

```
$ repodoctor
repodoctor: 5 checks

  ✓ Node >=12.0.0 required (package.json engines), found 26.10.0
  ✗ yarn is this repo's package manager but is not installed
      fix: npm install -g yarn
  ✗ Node dependencies not installed (no node_modules folder)
      fix: yarn install
  ✗ No .env file; .env.example defines 12 variable(s)
      fix: cp .env.example .env, then fill in the values
  ✗ MongoDB (docker-compose.yml) is not reachable on localhost:27017
      fix: docker compose up -d

4 failed, 0 warning(s), 1 passed
```

That is real output from a fresh clone of `hagopj13/node-express-boilerplate`.

## Install

Not on PyPI yet. From a copy of this folder:

```
pip install .
```

It is one file with no dependencies, so `python repodoctor.py` also works.

## Use

```
repodoctor            # check the current folder
repodoctor path/to/repo
repodoctor --ci       # for pipelines, see below
```

The exit code is 1 if any check failed. It only reports; it never changes anything on your machine.

## What it checks

| Check | Read from |
|---|---|
| Node version | `.nvmrc`, `.node-version`, `package.json` engines |
| Python version | `.python-version`, `runtime.txt`, `pyproject.toml` |
| Package manager is installed | lockfile or `packageManager` (pnpm, yarn, bun, uv, poetry, pipenv) |
| Dependencies are installed | `node_modules`; `.venv` or `venv` |
| Env vars are set | every `process.env.X`, `import.meta.env.X`, `os.environ[...]`, `os.getenv(...)` in the source, plus `.env.example` |
| Env vars are documented | the same scan, compared with `.env.example` |
| Services are running | Postgres, Redis, MongoDB and MySQL ports in the compose file, or their drivers in the dependencies |

A variable read with a fallback (`os.getenv("X", "default")`, `process.env.X || ...`) counts as optional and is not reported. Tests, docs, examples and installed packages are not scanned.

## CI mode

`repodoctor --ci` ignores the machine and fails if the code reads an environment variable that is not in `.env.example`. Run it on every pull request and the example file cannot fall behind the code.

## Limits

- Node and Python only.
- It checks the folder you point it at. In a monorepo, run it in each project folder.
- Env var names built at runtime, or read through a settings library (Joi, pydantic-settings, django-environ), are not seen by the scan. The `.env.example` check still covers them.
- Compose files are read by pattern, so ports written as `${VAR:-5432}:5432` are missed.
- A missing Python virtualenv is a warning, because poetry and pipenv keep theirs outside the repo.
- Anything that lives outside the code, such as VPN access or an account someone has to create for you, cannot be detected.

## Tests

```
python test_repodoctor.py
```
