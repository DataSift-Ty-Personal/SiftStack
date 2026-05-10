"""External source adapters for the deep_prospecting pipeline.

Each module wraps one external data source (Find-A-Grave, TruePeopleSearch,
etc.) behind a typed interface that returns deep_prospecting model types.
Phases import from `sources/`; `sources/` never imports from `phases/`.
"""
