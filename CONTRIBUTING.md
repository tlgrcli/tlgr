# Contributing to tlgr

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Getting Started

1. Fork the repository
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/tlgr.git
   cd tlgr
   ```
3. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Development Guidelines

### Code Style

- Follow PEP 8 style guidelines
- Use type hints for function parameters and return values
- Add docstrings to all public functions and classes
- Keep lines under 100 characters

### Commit Messages

- Use clear, descriptive commit messages
- Start with a verb in present tense (e.g., "Add", "Fix", "Update")
- Reference issue numbers when applicable (e.g., "Fix #123")

### Testing

The Makefile owns the gates, and CI runs the same targets. Before submitting
a PR, run what CI will run:

```bash
make check
```

That is `lint`, `typecheck`, `test`, `docs` and `parity` in order. Two
shorter forms exist for the inner loop: `make test-fast` (no coverage, stops
at the first failure) and `make acceptance` (the subset that proves
ARCHITECTURE 12.3).

The generated reference and the parity index are artefacts, not hand-written
files. `make docs` and `make parity` regenerate them; `tests/test_docs_fresh.py`
and `tests/test_parity.py` fail if you commit code without them.

Manual verification against a test Telegram account is still worth doing for
anything that touches the wire, but it is not a substitute for the suite.

### Pull Requests

1. Create a new branch for your feature/fix:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and commit them

3. Push to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```

4. Open a Pull Request against the `main` branch

5. Fill out the PR template with details about your changes

Branch names matter to CI: `feat/**`, `fix/**`, `chore/**`, `docs/**` and
`design/**` are built on push. A branch outside those prefixes is only built
once its pull request is open.

## Releases

A release is a tag, and the tag is the whole procedure. Three files state the
version and all three have to agree before anything is published:
`tlgr/__init__.py` (`__version__`, which `pyproject.toml` reads), the
`## [x.y.z]` heading in `CHANGELOG.md`, and the tag itself.

1. Bump `__version__` and write the `CHANGELOG.md` entry, in a PR like any
   other change.
2. Once it is on `main` and CI is green there:
   ```bash
   git tag -a v2.0.0 -m "v2.0.0"
   git push origin v2.0.0
   ```

The `release` workflow takes it from there. It checks the three versions
against each other, re-runs the acceptance suite against the tagged tree,
builds the sdist and the wheel, installs the wheel into a clean environment
and asks it for its version, and only then creates the GitHub release with
the changelog section as its notes and both artefacts attached. Any step
failing means no release is created, so a bad tag costs a `git push --delete`
and nothing else.

A second job publishes those same two files to PyPI. It downloads the
artefacts the first job built rather than rebuilding them, because the wheel
that was smoke-tested and the wheel that reaches PyPI have to be the same
bytes. It runs after the GitHub release exists, so a PyPI failure leaves the
release standing and is re-runnable on its own.

### The PyPI publisher

Publishing uses [trusted
publishing](https://docs.pypi.org/trusted-publishers/): PyPI verifies the
workflow's OIDC token instead of an API token, so there is no publishing
secret in this repository and nothing to leak or rotate. It has to be
configured once, on pypi.org, before the first release that uses it:

- owner `tlgrcli`, repository `tlgr`, workflow `release.yml`, environment
  `pypi`;
- for the first release, add it as a *pending* publisher, since the project
  does not exist on PyPI until something is published to it.

Until that publisher exists the `publish` job fails and the GitHub release
still succeeds, which is the intended order: the release is the artefact of
record, PyPI is a distribution channel on top of it.

## Reporting Issues

When reporting bugs, please include:

- Python version (`python --version`)
- Telethon version (`pip show telethon`)
- Operating system
- Steps to reproduce the issue
- Expected vs actual behavior
- Any error messages or logs

## Feature Requests

Feature requests are welcome! Please:

- Check if the feature has already been requested
- Provide a clear description of the feature
- Explain why it would be useful

## Security

If you discover a security vulnerability, please do NOT open a public issue. Instead, contact the maintainer directly.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
