# Contributing

Issues and pull requests are welcome. Before opening a pull request run `make lint`,
`make test` and `make validate`; all three must pass.

## Adding a task family

A family is a function in [src/triagebench/tasks/catalog.py](src/triagebench/tasks/catalog.py)
that returns a list of `TaskSpec`, usually three variants.

1. **Pick the condition.** Conditions are dataclasses in
   [src/triagebench/tasks/spec.py](src/triagebench/tasks/spec.py): `LateFile`, `TaskFailure`,
   `BadCommit`, `ConfigCommit`, `Cents`, `Rename`, `Truncate`, `CdcReplay`, `Discontinue`,
   `Announce`. Put the condition in `faults` if it should exist only in the episode world, or in
   `benign` if the expected end state should include it too (noise, business changes).
   A new kind of condition needs a dataclass in `spec.py` and handling in
   `apply_file_conditions` or `run_window` in
   [src/triagebench/tasks/build.py](src/triagebench/tasks/build.py).
2. **Choose dates.** `incident_ds` is the partition the on-call looks at; the episode starts at
   08:30 the next calendar day. `window_start` is the first business day the scheduler re-runs.
   Avoid the day after a bank holiday as an incident date unless that is the point.
3. **Write the answer.** `decision` (one of the seven classes), `root_cause` (from
   `root_cause_vocabulary()`), `reference_mode` (`clean`, `initial` or `quarantine`),
   `required_escalation`, `monitor_requirements` and `allowed_monitor_changes` where they apply.
4. **Write the action rules.** `acceptable` lists every write action a correct resolution may
   use; anything else is unnecessary. `forbidden` lists actions that are never acceptable for
   this task (destructive tools are forbidden everywhere already).
5. **Write the oracle plan.** `oracle` is a list of tool calls that solves the task through
   the tool API. `{bad_sha}` and `{config_sha}` are filled from the generated world.
6. **Name the alerts.** `expected_alerts` is the set of monitors that must fire, no more and no
   fewer, including any noise.
7. **Register the family** in `FAMILIES` and in `all_tasks()`, add it to
   [docs/tasks.md](docs/tasks.md), and update the counts in `tests/test_unit.py`.
8. **Check it:**

   ```bash
   uv run triagebench generate --tasks <family>      # reports alert mismatches
   uv run triagebench run --agent oracle --tasks <family>
   make validate                                     # oracle 100%, alerts, leakage, determinism
   make baselines                                    # refresh results/
   ```

Keep names that reach the world plausible: commit messages, log lines, file names and
announcements are read by the agent. The leakage check fails if a task id, family id, task
summary or decision identifier appears in any tool output.

## Changing the world

Anything that changes generated worlds (data generation, the pipeline repository template,
the scheduler, monitors, fingerprints) must bump `BUILD_VERSION` in
`src/triagebench/tasks/build.py` so cached worlds are rebuilt, and the baseline results must
be regenerated.
