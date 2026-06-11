# Contributing to orkoprox

Thank you for considering a contribution. This document covers how to set up a dev environment, run tests and linters, and submit a pull request.

---

## Prerequisites

- Python 3.11 or newer
- Docker (for integration tests and local compose stack)
- Redis (optional — start via `docker compose up redis` for quota-related tests)

---

## Dev Setup

```bash
# Clone and enter the repo
git clone https://github.com/truecode-org/orkoprox.git
cd orkoprox

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install the package in editable mode with dev dependencies
pip install -e ".[dev]"
# or, if you use uv:
# uv sync --group dev
```

Copy the example config:

```bash
cp .env.example .env
# Edit .env — at minimum set PROXY_API_KEYS and your provider credentials
```

Start the gateway locally:

```bash
make dev
# or directly:
uvicorn app.main:app --host 0.0.0.0 --port 8091 --reload
```

---

## Running Tests

```bash
make test
# or:
pytest -q
```

Tests that hit a live provider are skipped by default. To run them, set the relevant provider ENV vars and run:

```bash
make test-live-compat
```

---

## Linting and Formatting

We use [ruff](https://docs.astral.sh/ruff/) for both linting and formatting.

```bash
make lint          # check only
ruff check app tests

ruff format app tests   # auto-format
```

Code style rules:

- **Spaces only** — never tabs (except in Makefile recipe lines, which require tabs by make syntax).
- **Line length: 100** characters (configured in `pyproject.toml`).
- **Target: Python 3.11+** — no walrus operators in places that need 3.10 compat, etc.
- **No bare `except:`** — always catch specific exceptions.
- All linting must pass before a PR is merged. CI enforces this.

---

## Docker Build

```bash
make docker-build
# produces: orkoprox:local

make docker-run
# runs the image with your local .env
```

---

## Pull Request Process

1. Fork the repository and create a branch from `main`.
2. Make your changes. Add or update tests as appropriate.
3. Run `make lint` and `make test` — both must pass.
4. Open a PR against `main` with a clear description of what changed and why.
5. A maintainer will review within a reasonable time. Feedback will be direct and constructive.

**Branch naming** (recommended, not enforced): `fix/<short-description>`, `feat/<short-description>`, `docs/<short-description>`.

**Commit style** (optional): [Conventional Commits](https://www.conventionalcommits.org/) are welcome but not required. Clear imperative-mood messages are enough (`Add fallback chain timeout`, `Fix quota header missing on streaming responses`).

---

## What to Contribute

Particularly welcome:

- Bug fixes with a regression test.
- Provider compatibility fixes (new quirks, new model shapes).
- Documentation improvements.
- Tests for edge cases in routing, quota accounting, or guard layer.

For larger features (new endpoints, new provider shapes, architectural changes), please open an issue first to discuss scope and design before writing code. This avoids duplicate effort.

---

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md). By participating you agree to abide by its terms.
