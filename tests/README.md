# Test Data

Fixtures in this folder use fictional people, addresses, phone numbers and record numbers. Where a test was built from a real notice or record, it keeps the real layout and wording and replaces the parties.

`obituary_ground_truth.json` is an optional, local-only file: real obituary and public-notice records used to measure the obituary enrichment pipeline's accuracy. It is not part of the public release. The scripts that read it (`test_ancestry.py --batch`, `src/obituary_experiment.py --test-set`) need your own copy; without it, `test_ancestry.py` skips the batch run with a message.
