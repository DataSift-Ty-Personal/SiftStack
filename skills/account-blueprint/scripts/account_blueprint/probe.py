"""Create with a read-back, and a probe protocol for routes never exercised
before this tool.

Four create routes were unverified when this was written (task-group,
task-preset, sequence-folder, custom-field group). The protocol: the FIRST
real object of the family is POSTed, read back by title, and only then does
the batch continue. A 404 or 405 marks the family manual: every intended
payload lands in the report as a configure-by-hand item and the other
families keep going. A 400 stops the family with the body verbatim, because
that is a payload-shape problem, not a missing route.

There are no sentinel objects: the DELETE routes are equally unverified and a
sentinel could be left behind in a stranger's account.
"""
from __future__ import annotations

import json

from .client import ApiError, NetworkError

UNVERIFIED = {
    "task_group": "/api/internal/task-group/",
    "task_preset": "/api/internal/task-group/{g}/task-preset/",
    "sequence_folder": "/api/internal/sequence-folder/",
    "custom_field_group": "/api/internal/custom-fields/group/",
    "siftline_board": "/api/internal/siftline/board/",
    "siftline_column": "/api/internal/siftline/board/{b}/column/",
}


def create(ctx, family: str, route_key: str, path: str, body: dict, title: str,
           readback, *, unique_title: bool = False):
    """POST body, then readback() -> object or None. Returns the read-back
    object, or None when nothing was (or should be) created.

    readback must re-LIST from the server and find the object by title; the
    POST response alone does not prove the server stored what was sent.
    """
    log, routes = ctx.log, ctx.routes
    if ctx.opts.verify_only:
        log.add(family, "gap", title, "missing in target")
        return None
    if not ctx.opts.commit:
        log.add(family, "created", title, "(dry run) POST %s" % path)
        return {"uuid": "DRY-RUN-%s" % title, "_dry": True}
    if routes.get(route_key) == "manual":
        log.add(family, "manual_todo", title, "create by hand (route %s unavailable): %s"
                % (path, json.dumps(body, ensure_ascii=False)[:600]))
        return None
    if routes.get(route_key) == "failed":
        log.add(family, "manual_todo", title, "create by hand (route %s rejected the payload): %s"
                % (path, json.dumps(body, ensure_ascii=False)[:600]))
        return None
    if ctx.opts.probe_only and route_key in ctx.probed:
        log.add(family, "skipped", title, "--probe-only: one object per route")
        return None
    probing = route_key in UNVERIFIED and routes.get(route_key) != "verified"
    if probing:
        log.add(family, "probe", title, "first create on unverified route POST %s" % path)
    try:
        ctx.client.call(path, "POST", body)
    except NetworkError as e:
        # The server may have committed. For families without a unique title
        # constraint a blind retry double-creates, so look first.
        got = readback()
        if got:
            log.add(family, "warn", title, "network error after POST but object present: %s" % e)
            return _verified(ctx, family, route_key, title, got)
        raise
    except ApiError as e:
        if probing and e.code in (404, 405):
            routes[route_key] = "manual"
            log.add(family, "probe", title, "POST %s -> %s: route unavailable, family MANUAL" % (path, e.code))
            log.add(family, "manual_todo", title, "create by hand (route %s -> %s): %s"
                    % (path, e.code, json.dumps(body, ensure_ascii=False)[:600]))
            return None
        if probing and e.code == 400:
            routes[route_key] = "failed"
            log.add(family, "probe", title, "POST %s -> 400: %s" % (path, e.body[:300]))
            log.add(family, "manual_todo", title, "create by hand (payload rejected: %s): %s"
                    % (e.body[:200], json.dumps(body, ensure_ascii=False)[:600]))
            return None
        if unique_title and e.code == 400 and ("exist" in e.body.lower() or "unique" in e.body.lower()):
            got = readback()
            if got:
                log.add(family, "exists", title, "server says the title already exists")
                return got
        log.add(family, "mismatch", title, "POST %s -> HTTP %s: %s" % (path, e.code, e.body[:300]))
        ctx.failed = True
        return None
    got = readback()
    if not got:
        log.add(family, "mismatch", title, "POST %s returned but the object is not on re-list" % path)
        ctx.failed = True
        return None
    if probing:
        routes[route_key] = "verified"
        log.add(family, "probe", title, "POST %s -> read-back OK, route verified" % path)
    ctx.probed.add(route_key)
    return _verified(ctx, family, route_key, title, got)


def _verified(ctx, family, route_key, title, got):
    ctx.log.add(family, "created", title, "uuid %s" % str(got.get("uuid") or got.get("id"))[:8])
    return got
