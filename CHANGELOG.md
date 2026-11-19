# Changelog

## 0.1.0 - 2026-11-19

First release.

- Harbor Lending world: four sources with contracts and owners, DuckDB warehouse, dbt project
  (dbt-core 1.12, dbt-duckdb 1.11), a small scheduler with task records and logs, six kinds of
  monitor, and a pipeline git repository with history. Generated deterministically from a seed.
- 33 tasks in 11 families, each with ground truth kept outside the episode.
- Tool API with 20 read tools, 8 write tools, 3 guarded destructive tools and `submit`;
  served over MCP (stdio) and callable directly; every call traced to JSONL.
- Scoring of decision, root cause, end state and restraint; aggregate report by family.
- Oracle and four naive baselines; baseline results in `results/`.
- LLM runner for Claude models (not run).
- `make validate`: oracle, alert, leakage, incremental-build and determinism checks.
