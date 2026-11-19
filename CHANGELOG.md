# Changelog

## 0.1.1 - 2026-11-19

- First runs of Claude models: Opus 5.5 resolves 31 of 33 tasks ($5.40), Sonnet 5.5 at medium
  effort 28 of 33 ($1.52). Scores, traces and transcripts in `results/llm/`.
- The `rerun` and `backfill` definitions, in the `submit` tool and the on-call runbook, no
  longer overlap: a missing input that has arrived since is `backfill`; a transient failure,
  or a wrong input that has been re-delivered, is `rerun`. Found by the first live call.
  Expected decisions and baseline scores are unchanged.
- Tool arguments are checked against their JSON types; a string passed where a list is
  expected is refused instead of being split into characters.
- The world cache version includes a hash of the repository template, so editing a runbook
  or the dbt project rebuilds cached worlds.

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
