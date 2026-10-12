# setupdoctor

[![tests](https://github.com/zamudiog1221-ops/setupdoctor/actions/workflows/tests.yml/badge.svg)](https://github.com/zamudiog1221-ops/setupdoctor/actions/workflows/tests.yml)

`brew doctor` for any repo. It reads what a project needs from its code and config files, checks your machine, and prints the fix for each gap. It never reads the README.

![setupdoctor running after git clone: one check passes, four fail with their fixes, then it offers to run them](https://raw.githubusercontent.com/zamudiog1221-ops/setupdoctor/main/demo.svg)

The animation re-creates setupdoctor's real output for `hagopj13/node-express-boilerplate`. The same checklist as text:

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

## Install

```
pip install setupdoctor
```

It needs Python 3.9 or newer and has no dependencies. The tests run on Linux, macOS and Windows on every push.

## Use

```
setupdoctor            # check the current folder
setupdoctor path/to/repo
setupdoctor --ci       # for pipelines, see below
```

The exit code is 1 if any check failed.

## It offers to fix what it can

After the checklist, setupdoctor lists the fixes it can run itself and asks before doing anything:

```
setupdoctor can run these for you:
  npm install -g yarn
  yarn install
  copy .env.example .env
  docker compose up -d
Run them now? [y/N]
```

Answer `y` and it runs them in order, stops at the first one that fails, then checks again. Anything else, including just pressing Enter, changes nothing. It never asks in CI, with `--ci`, or when the output is going to a file.

What it can run: installing the package manager, installing dependencies, creating a Python virtualenv, copying `.env.example` to `.env` (never over an existing file), and `docker compose up -d`.

If npm's global folder is not on your PATH, which is common on Windows machines set up without admin rights, `npm install -g yarn` succeeds but `yarn` is still not found. setupdoctor detects that, runs yarn from that folder itself, and tells you which folder to add to your PATH.

What it leaves to you:

- A wrong Node or Python version. Version managers work by changing your terminal session, which a program can't do for you. Dependencies are not installed until the version is right.
- Secret values. It creates `.env`, but only you know what goes in it.
- Installing Node or Docker themselves.

The commands come from a fixed list inside setupdoctor, never from text in the repo. Installing a project's dependencies does run that project's own install scripts, as `npm install` always does, so answer `n` for a repo you don't trust.

## Run it automatically after every clone

```
setupdoctor --install-hook
```

Do this once. From then on, every `git clone` prints the checklist for the new repo as soon as the clone finishes, followed by the same offer to fix things. It stays silent on repos with nothing to check and when you switch branches.

If you installed the hook with a version before 0.2.0, upgrade and run `setupdoctor --install-hook` again. The old hook started Python in a way that let a file in the cloned repo run as code.

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

## Related tools

If a project already ships a Dev Container, a Nix flake or a `mise` config, use that: it sets the environment up for you. setupdoctor is for the projects that don't, which is most of them, and it needs nothing added to the repo.

`npm doctor` checks your npm installation, not a particular project. A few small tools check some of the same things setupdoctor does, such as [env-doctor](https://github.com/thehypewipe/env-doctor) and [forkready](https://github.com/AmarReddy4/repo-scout).

## False alarms

A wrong result is the worst thing this tool can do. It has been run against 21 open-source projects and tuned until every failure it reported was real, but that is a small sample. If it reports something that isn't a problem, or misses something that is, please open an issue naming the repo.

## Tests

```
python test_setupdoctor.py
```
