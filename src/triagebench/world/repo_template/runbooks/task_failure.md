# Task failures

1. Read the task log for every attempt. Note the exception and where it happened.
2. Transient errors (database lock, network timeout, worker eviction) with good input:
   re-run the task.
3. Input errors (missing file, schema mismatch, trailer mismatch): look at the landing
   zone. A corrected re-delivery (`_r<n>` suffix) supersedes the original; re-run the
   ingest once it has arrived. Without one, escalate to the source owner.
4. Code errors: find the change that introduced them.
