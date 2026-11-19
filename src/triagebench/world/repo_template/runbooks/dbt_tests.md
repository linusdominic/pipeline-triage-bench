# dbt test failures

`dbt_build` runs `dbt build` for one partition; a failing test stops the models
downstream of it for that run. Results are in `ops.dbt_test_results`.

1. Which test failed, on which model, for which partitions, since when?
2. Look at the failing rows (query the model).
3. Did the model's code change recently? `git log` on dbt/models. A bad change is fixed
   by reverting or correcting it and rebuilding every partition it touched.
4. Is the input wrong instead? Check the raw partition and the source contract.
5. Relationship warnings on branch codes usually mean dim_branch is missing branches:
   check which branch snapshot is loaded and what has arrived in landing/branches.
