# Reconciliation alerts

The reconciliation monitor compares what was loaded into the raw table for a partition
(record count and the sum of the amount column) with the sender's trailer record.

1. Compare loaded rows with the landing file: same rows, same values?
2. Compare values with the contract's units and expected ranges.
3. If the delivered values are wrong, the data must not stay in the marts: quarantine the
   raw partition and escalate to the owner. Do not correct values yourself.
