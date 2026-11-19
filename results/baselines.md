# Baseline scores

Produced by `triagebench baselines --jobs 4 --seed 7` on 2026-11-19 (Darwin arm64, Python 3.12.16). 165 episodes in 48.4s wall time with 4 worker processes.

| Agent | Resolved | Decision | Root cause | End state | False-alarm restraint | Unnecessary-action rate | Destructive-action rate | Mean tool calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| oracle | 100% | 100% | 100% | 100% | 100% | 0% | 0% | 4.39 |
| always_rerun | 15% | 15% | 6% | 52% | 0% | 58% | 0% | 4.48 |
| always_escalate | 12% | 12% | 67% | 39% | 0% | 79% | 0% | 3.0 |
| always_close | 27% | 27% | 64% | 27% | 100% | 0% | 0% | 4.61 |
| fix_everything | 6% | 15% | 52% | 39% | 0% | 73% | 9% | 7.7 |

## Resolved rate by family

| Family | oracle | always_rerun | always_escalate | always_close | fix_everything |
| --- | --- | --- | --- | --- | --- |
| cdc_replay | 100% | 0% | 0% | 100% | 0% |
| cents | 100% | 0% | 0% | 0% | 0% |
| discontinuation | 100% | 0% | 0% | 100% | 0% |
| fanout | 100% | 0% | 0% | 0% | 0% |
| holiday | 100% | 0% | 0% | 100% | 0% |
| late_file | 100% | 0% | 0% | 0% | 33% |
| rename | 100% | 0% | 100% | 0% | 0% |
| stale_reference | 100% | 0% | 0% | 0% | 0% |
| tight_config | 100% | 0% | 0% | 0% | 33% |
| transient | 100% | 100% | 0% | 0% | 0% |
| truncated | 100% | 67% | 33% | 0% | 0% |
