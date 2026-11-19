.PHONY: setup generate validate baselines test lint clean

JOBS ?= 4

setup: ## Python 3.12 and every dependency (dev tools and the llm extra)
	uv sync --all-extras

generate: ## base world and all task worlds, cached under .cache/triagebench
	uv run triagebench generate --jobs $(JOBS)

validate: ## oracle 100%, determinism, leakage, alerts fire, incremental equals full refresh
	uv run triagebench validate --jobs $(JOBS) --report results/validation.json

baselines: ## every scripted agent on every task; writes results/baselines.*
	uv run triagebench baselines --jobs $(JOBS)

test: ## no network, no API key
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

clean:
	rm -rf .cache runs .pytest_cache .ruff_cache .mypy_cache
