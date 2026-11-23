"""Links into the DataSift web app (app.reisift.io), one definition for every module.

Checked against the app's own route table (its public bundle, main.min.js, 2026-11-17): a property
record is the route "/records/properties/:uuid", and that is the link the app itself builds wherever it
points at a record. "/records/<uuid>" is NOT a record link: it matches the records LIST route
("/records/:type") and opens the wrong page. Five modules built that short form until this file existed.
An owner is "/records/owners/:uuid".

    record_url("c0ffee00-...")   -> https://app.reisift.io/records/properties/c0ffee00-...
    slack_link(uuid, "100 Example St")  -> <https://...|100 Example St>, Slack's link markup
"""
from __future__ import annotations

APP = "https://app.reisift.io"
RECORD = APP + "/records/properties/%s"
OWNER = APP + "/records/owners/%s"


def record_url(uuid: str) -> str:
    """The record's page, or "" when there is no uuid (never a link to the list)."""
    u = str(uuid or "").strip()
    return RECORD % u if u else ""


def owner_url(uuid: str) -> str:
    u = str(uuid or "").strip()
    return OWNER % u if u else ""


def slack_link(uuid: str, label: str) -> str:
    """Slack's link markup for a record, labelled with the street; the bare label when there is no uuid."""
    url = record_url(uuid)
    text = str(label or "").replace("|", "/").replace(">", "").replace("<", "")
    return "<%s|%s>" % (url, text) if url and text else (url or text)
