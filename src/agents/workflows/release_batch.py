"""release_batch: the worker's half of releasing a staged batch from Slack (see agents/release.py).

Not on the roster on purpose: no employee has it as a tool, so only a named person's confirm in
Slack can queue it.
"""
from __future__ import annotations

from .. import release


def run(task: dict) -> dict:
    return release.task_run(task)


def render(task: dict, result: dict) -> str:
    return release.task_render(task, result)
