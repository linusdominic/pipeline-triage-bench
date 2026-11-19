# Volume anomaly alerts

A volume monitor compares a partition's row count with the median of the previous
`baseline_partitions` partitions and fires when the relative difference exceeds
`threshold`.

1. Is the change real or an artefact? Query the partition: duplicated keys, a replay of
   events, a truncated load.
2. Does the layer below or above show the same change? A raw-table anomaly that staging
   removes (for example CDC replays) does not reach the marts.
3. Is there a business reason? Check the announcements (product changes, branch changes).
   A known, announced change is not an incident; you may set the monitor's
   `baseline_start` to the change date so the baseline re-forms.
4. Did the monitor's configuration change recently? Check git history of monitoring/.
