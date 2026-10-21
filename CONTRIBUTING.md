# Contributing to SmutWorks

SmutWorks is in alpha. Please target code pull requests at **`develop`**. Project landing-page and branding changes can target **`main`**.

## Before opening a pull request

1. Check existing issues and describe the problem or desired behavior.
2. Keep changes focused. Include tests for logic that changes file handling, Stash mutations, encoding decisions, or recovery behavior.
3. Run the quality checks and tests below from the `develop` checkout.
4. Update the relevant documentation when behavior or setup changes.

The application uses Python's standard library and supports Python 3.8+. Development tools are separate and require Python 3.11+ and Node.js 24+. Avoid introducing an application dependency without discussing the need first.

## Developer setup

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm ci
pre-commit install
```

`pre-commit install` installs both the code checks and the commit-message hook. Keep the virtual environment active when committing; the hooks use its pinned tools. Node is needed for contributor commit checks only, not to run SmutWorks. No hook installation is required for normal app users.

## Quality checks

```sh
python -m ruff check .
python -m black --check .
python -m mypy
python -m unittest discover -v
```

To fix lint issues and format Python files:

```sh
python -m ruff check --fix .
python -m black .
```

Black owns formatting; Ruff checks errors, unused imports, and import order. Both target Python 3.8 syntax. mypy checks all nine application modules, including unannotated function bodies. This is gradual typing, not strict typing: JSON/API data remains dynamic, and annotations can be strengthened as code changes. Tests are linted and formatted, but are not part of the mypy target.

Run `pre-commit run --all-files` to check all tracked Python files locally. Hooks may fix files; review and stage those edits before committing again. CI checks formatting without rewriting files, runs mypy, and retains the macOS unit/package smoke tests.

## Commit messages

Use Conventional Commits, for example:

```text
feat: add duplicate keeper recommendations
fix(encoding): preserve output when the network share disconnects
docs: clarify Stash path mapping
chore: update development tools
```

commitlint checks messages locally and checks the full new commit range for pushes and pull requests in CI (the latest commit for a newly created branch). Existing history is unchanged; when bringing older nonconforming commits into a new pull request, squash or reword them on your topic branch before opening it. Do not rewrite shared `main` or `develop` history. To check one message manually:

```sh
printf '%s\n' 'feat: add duplicate review' | npm run commitlint
```

## Working with media

Use generated test clips and temporary folders when possible. Never commit real library reports, API keys, settings, credentials, or personal media. Use mocked Stash responses for automated tests.

Preserve preview/apply behavior, source-file protection, collision checks, and the history needed to review an operation. Do not add automatic video deletion as an incidental part of another feature.

## Bug reports

Include the application version or commit, macOS version, action selected, expected result, actual result, and a small sanitized log excerpt. Encoding reports should include the encoder and HandBrakeCLI/ffprobe versions when available.

Please remove API keys, passwords, hostnames, addresses, usernames, and identifying media paths before posting. See [SECURITY.md](SECURITY.md) for sensitive reports.
