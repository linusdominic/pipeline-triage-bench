# Schema alerts

A schema monitor compares the header of the newest landing file for a source with the
columns in the source's contract.

1. Read the file header and the contract. Note missing and unexpected columns.
2. Check the announcements and the contract's change policy. Changes need notice.
3. A breaking change without the required notice is the sender's to fix: escalate to the
   owner with the file name and the column difference. Do not edit the mapping or the file.
