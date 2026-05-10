"""Deep prospecting — automated decision-maker research.

A self-contained sub-module of SiftStack that runs end-to-end research
on a single property/owner/docket and produces a verified decision-maker
plus full skip-trace results in a structured markdown research pack.

Entry points:
    python -m deep_prospecting --address "..."

Programmatic use is via `deep_prospecting.run()`, exported here once
the orchestrator lands. Slice 1 keeps this package surface deliberately
thin — only models leak out.
"""

# Slice 1: only the data shapes are public. CLI / orchestrator / phases
# come online in subsequent slices.
from deep_prospecting import models  # noqa: F401
