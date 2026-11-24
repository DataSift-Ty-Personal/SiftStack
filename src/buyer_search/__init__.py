"""Buyer search: the buyers most likely to buy one house.

  open_api.py  SiftStack's own version, on a user's DataSift Open API key (what users, Slack and the CLI run)
  engine.py    the data team's model (ported, reviewed and fixed 2026-11-23) and the scoring both share
  backends.py  the read-only MCP the model reads the property cluster through (staff only)
  client.py    runs either, and marks the buyers SiftStack never sells to
  fakes.py     an in-memory cluster and invented data for the tests

Command line: python src/buyer_search_cli.py --help. In Slack: ask Roma for the buyers on a house
(workflow buyer_search).
"""
from .engine import FIXES, ORIGIN, VERSION  # noqa: F401
