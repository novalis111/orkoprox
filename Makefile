.PHONY: dev lint format test docker-build docker-run ci

UVICORN := uvicorn
PYTEST := pytest
RUFF := ruff

ifneq ("$(wildcard .venv/bin/python)","")
UVICORN := .venv/bin/python -m uvicorn
PYTEST := .venv/bin/python -m pytest
RUFF := .venv/bin/python -m ruff
endif

dev:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8091 --reload

lint:
	$(RUFF) check app tests

format:
	$(RUFF) format app tests

test:
	$(PYTEST) -q

docker-build:
	docker build -t orkoprox:local .

docker-run:
	docker run --rm -p 8091:8091 --env-file .env orkoprox:local

ci:
	$(MAKE) lint
	$(MAKE) test
