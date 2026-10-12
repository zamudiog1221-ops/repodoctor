# setupdoctor

`brew doctor` for any repo. It reads what a project needs from its code and config files, checks your machine, and prints the fix for each gap. It never reads the README.

```
$ setupdoctor
setupdoctor: 5 checks

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

It is one file with no dependencies, so `python setupdoctor.py` also works.

## Use

```
setupdoctor            # check the current folder
setupdoctor path/to/repo
setupdoctor --ci       # for pipelines, see below
```

The exit code is 1 if any check failed. A check only reports; it never changes anything on your machine.

## Run it automatically after every clone

```
setupdoctor --install-hook
```

Do this once. From then on, every `git clone` prints the checklist for the new repo as soon as the clone finishes. It stays silent on repos with nothing to check and when you switch branches.

This is the one command that changes your machine: it adds a `post-checkout` hook to git's template folder and points `init.templateDir` in your global git config at it. Repos you cloned earlier are not affected. Undo it with `setupdoctor --uninstall-hook`.

## What it checks

| Check | Read from |
|---|---|
| Node version | `.nvmrc`, `.node-version`, `package.json` engines |
| Python version | `.python-version`, `runtime.txt`, `pyproject.toml` |
| Package manager is installed | lockfile or `packageManager` (pnpm, yarn, bun, uv, poetry, pipenv) |
| Dependencies are installed | `node_modules`; `.venv` or `venv` |
| Env vars are set | `.env.example`, plus reads in the source that break when the variable is unset |
| Env vars are documented | every env var the source reads, compared with `.env.example` |
| Services are running | Postgres, Redis, MongoDB and MySQL ports in the compose file |

It only fails a check when the repo declares the requirement. For environment variables that means one of:

- the variable is in `.env.example` and you have no `.env`
- Python reads it as `os.environ["X"]` outside any `if` or `try`
- TypeScript reads it as `process.env.X!`

Softer reads such as `os.getenv("X")` or a bare `process.env.X` are treated as settings the app can run without. On real projects they nearly always are, and reporting them buried the real problems under dozens of false alarms. Tests, docs, examples and installed packages are not scanned.

## CI mode

`setupdoctor --ci` ignores the machine and fails if the code reads an environment variable that is not in `.env.example`. Run it on every pull request and the example file cannot fall behind the code.

## Limits

- Node and Python only.
- It checks the folder you point it at. In a monorepo, run it in each project folder.
- Env var names built at runtime, or read through a settings library (Joi, pydantic-settings, django-environ), are not seen by the scan. The `.env.example` check still covers them.
- A JavaScript project with no `.env.example` gets no env var checks, because nothing in plain JavaScript marks a variable as required.
- A database is only checked if the compose file publishes its port. One you are expected to install yourself is not detected.
- Compose files are read by pattern, so ports written as `${VAR:-5432}:5432` are missed.
- A missing Python virtualenv is a warning, because poetry and pipenv keep theirs outside the repo.
- Anything that lives outside the code, such as VPN access or an account someone has to create for you, cannot be detected.

## Tests

```
python test_setupdoctor.py
```
