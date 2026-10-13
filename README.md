# setupdoctor

[![tests](https://github.com/zamudiog1221-ops/setupdoctor/actions/workflows/tests.yml/badge.svg)](https://github.com/zamudiog1221-ops/setupdoctor/actions/workflows/tests.yml)

`brew doctor` for any repo. It reads what a project needs from its code and config files, checks your machine, and prints the fix for each gap. It never reads the README.

Works with Node, Python, PHP, Ruby, Go, Rust, Java and .NET projects, including monorepos.

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

What it can run: installing a Node or Python package manager, installing Node, Python or PHP dependencies, creating a Python virtualenv, copying `.env.example` to `.env` (never over an existing file), and `docker compose up -d`.

If npm's global folder is not on your PATH, which is common on Windows machines set up without admin rights, `npm install -g yarn` succeeds but `yarn` is still not found. setupdoctor detects that, runs yarn from that folder itself, and tells you which folder to add to your PATH.

What it leaves to you:

- A wrong language version. Version managers work by changing your terminal session, which a program can't do for you. Dependencies are not installed until the version is right.
- Secret values. It creates `.env`, but only you know what goes in it.
- Installing a language runtime or Docker themselves.

The commands come from a fixed list inside setupdoctor, never from text in the repo. Installing a project's dependencies does run that project's own install scripts, as `npm install` always does, so answer `n` for a repo you don't trust.

## Run it automatically after every clone

```
setupdoctor --install-hook
```

Do this once. From then on, every `git clone` prints the checklist for the new repo as soon as the clone finishes, followed by the same offer to fix things. It stays silent on repos with nothing to check and when you switch branches.

If you installed the hook with a version before 0.2.0, upgrade and run `setupdoctor --install-hook` again. The old hook started Python in a way that let a file in the cloned repo run as code.

This is the one command that changes your machine: it adds a `post-checkout` hook to git's template folder and points `init.templateDir` in your global git config at it. Repos you cloned earlier are not affected. Undo it with `setupdoctor --uninstall-hook`.

## What it checks

| Language | Version read from | Also checks | Can install |
|---|---|---|---|
| Node | `.nvmrc`, `.node-version`, `package.json` engines | package manager (npm, yarn, pnpm, bun), `node_modules` | package manager, dependencies |
| Python | `.python-version`, `runtime.txt`, `pyproject.toml` | package manager (uv, poetry, pipenv), virtualenv | package manager, virtualenv and dependencies |
| PHP | `composer.json` | composer, `vendor/autoload.php` | dependencies |
| Ruby | `.ruby-version`, `Gemfile` | bundler | |
| Go | `go.mod` | | |
| Rust | `rust-toolchain`, `Cargo.toml` | cargo | |
| Java | `.java-version`, `.sdkmanrc`, `pom.xml`, `build.gradle` | Maven or Gradle, unless the repo ships its wrapper | |
| .NET | `global.json`, the project file | | |

For every language it also checks:

- **Pinned tools.** Anything listed in `.tool-versions` or `mise.toml` (asdf and mise), including tools outside the table such as Terraform, Deno and Bun.
- **Env files.** `.env.example` and its variants (`.env.sample`, `.env.local.example`, `example.env` and others) against your `.env`.
- **Services.** Postgres, Redis, MongoDB and MySQL ports published in the compose file.

Go, Rust, Java and .NET fetch dependencies when you build, so there is nothing to check or install. Ruby's gems can only be checked by running bundler, which executes the project's `Gemfile`, and setupdoctor never runs a repo's code to check it.

### Monorepos

It looks for projects in the top folder and up to two levels below it, and labels each one:

```
  ✗ [backend] Python 3.12 required (.python-version), found 3.10.4
  ✗ [frontend] Node dependencies not installed (no node_modules folder)
```

Fixes run inside the folder they belong to. Workspace packages (a sub-folder in the same language as the top folder) are installed from the top, so they are not checked one by one, unless the folder has its own lockfile. A sub-folder with its own `.env.example` gets its env check there.

### Required environment variables

It only fails a check when the repo declares the requirement. For environment variables that means one of:

- the variable is in `.env.example` and you have no `.env`
- a settings schema marks it required: zod (including t3-env), envalid or Joi in JavaScript; pydantic-settings, django-environ or python-decouple in Python
- the code stops without it: `if (!process.env.X) throw ...`, TypeScript's `process.env.X!`, or Python's `os.environ["X"]` outside any `if` or `try`

Softer reads such as `os.getenv("X")` or a bare `process.env.X` are treated as settings the app can run without. On real projects they nearly always are, and reporting them buried the real problems under dozens of false alarms. Tests, docs, examples, deployment-only settings files and installed packages are not scanned.

## CI mode

`setupdoctor --ci` ignores the machine and fails if the code reads an environment variable that is not in `.env.example`. Run it on every pull request and the example file cannot fall behind the code. It reads env vars in all eight languages plus Kotlin.

## Limits

- Node and Python get the full treatment. The other six languages get version and tooling checks, and only PHP gets a dependency check.
- Projects nested more than two folders deep are not found. At most 12 sub-projects are checked.
- Env var names built at runtime are not seen. A JavaScript project with no `.env.example`, no settings schema and no guard clause gets no env var checks.
- On the interactive check, reading source files stops after 8 seconds and says so. On a huge repo, or a machine whose virus scanner checks every file, that can cut the env var scan short. `--ci` always reads everything.
- A compose file's databases are assumed to be needed. If the project treats them as optional, you get a failure you can ignore.
- A database is only checked if the compose file publishes its port. One you are expected to install yourself is not detected.
- Compose files are read by pattern, so ports written as `${VAR:-5432}:5432` are missed.
- A missing Python virtualenv is a warning, because poetry and pipenv keep theirs outside the repo.
- Anything that lives outside the code, such as VPN access or an account someone has to create for you, cannot be detected.

## Related tools

If a project already ships a Dev Container, a Nix flake or a `mise` config, use that: it sets the environment up for you. setupdoctor is for the projects that don't, which is most of them, and it needs nothing added to the repo.

`npm doctor` checks your npm installation, not a particular project. A few small tools check some of the same things setupdoctor does, such as [env-doctor](https://github.com/thehypewipe/env-doctor) and [forkready](https://github.com/AmarReddy4/repo-scout).

## False alarms

A wrong result is the worst thing this tool can do. It has been run against 78 open-source projects across the eight languages and tuned until every failure it reported was one the repo declares, but that is still a small sample. If it reports something that isn't a problem, or misses something that is, please open an issue naming the repo.

## Tests

```
python test_setupdoctor.py
```
