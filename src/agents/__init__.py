"""SiftStack agents: a four-employee AI team behind one always-on brain (build 1.0.63, 2026-11-11).

The brain (`brain.py`) takes Slack messages, DataSift sequence webhooks and the deploy's smoke test,
writes a task into a SQLite queue on its own volume, and a worker thread runs it: a wired workflow
directly, or an employee turn on the Claude Agent SDK (`runner.py`). Results go back to the DataSift
record over the Open API key and into the Slack thread under the employee's own name. Nothing is
shared between customers: each install is its own Fly app with its own volume, queue and keys.

The team lives in team/roster.yaml: Atlas (analysis), Diggs (research), Roma (sales) and Quinn (ops).
The first live workflow is Diggs's deep prospect, fired when a record's status changes to
"Deep Prospecting".
"""
