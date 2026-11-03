"""SiftStack agents: four autonomous agents behind one always-on brain (build 1.0.59, 2026-11-03).

The brain (`brain.py`) receives DataSift sequence webhooks, writes a task into a SQLite queue on
its own volume, and a worker thread runs the agent that owns the task, writes the result back to
the DataSift record over the Open API key, and posts to Slack. Nothing is shared between
customers: each install is its own Fly app with its own volume, queue and keys.

Agents: Analysis, Research, Sales Assistant, Operations. The first live workflow is the Research
agent's deep prospect, fired when a record's status changes to "Deep Prospecting".
"""
