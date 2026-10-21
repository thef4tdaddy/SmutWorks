# Contributing to SmutWorks

SmutWorks is in alpha. Please target code pull requests at **`develop`**. Project landing-page and branding changes can target **`main`**.

## Before opening a pull request

1. Check existing issues and describe the problem or desired behavior.
2. Keep changes focused. Include tests for logic that changes file handling, Stash mutations, encoding decisions, or recovery behavior.
3. Run `python3 -m unittest discover -v` from the `develop` checkout.
4. Update the relevant documentation when behavior or setup changes.

The application currently uses Python's standard library and supports Python 3.8+. Avoid introducing a dependency without discussing the need first.

## Working with media

Use generated test clips and temporary folders when possible. Never commit real library reports, API keys, settings, credentials, or personal media. Use mocked Stash responses for automated tests.

Preserve preview/apply behavior, source-file protection, collision checks, and the history needed to review an operation. Do not add automatic video deletion as an incidental part of another feature.

## Bug reports

Include the application version or commit, macOS version, action selected, expected result, actual result, and a small sanitized log excerpt. Encoding reports should include the encoder and HandBrakeCLI/ffprobe versions when available.

Please remove API keys, passwords, hostnames, addresses, usernames, and identifying media paths before posting. See [SECURITY.md](SECURITY.md) for sensitive reports.
