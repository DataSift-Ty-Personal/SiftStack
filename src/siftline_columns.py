"""SiftLine board columns on our own accounts: the one write the published API does not carry.

DataSift's published reference creates a board (POST /api/internal/siftline/board/) but has no
route that lists or creates a board's columns. The app does both through the board's column route,
and the account blueprint proved that route on our own accounts (2026-09-10: created and read back
on ty+1 with a login token). This module exists for one job: the First to Market board's seven
columns, made once from this workstation by src/agents/autopilot/pipelines.py.

It needs this workstation's own login: datasift_api_upload mints a token from the DataSift login
in .env when one is there. The Open API key cannot reach the column route, and the brain on Fly
never carries a password, so nothing the brain runs imports this module at load time (pipelines.py
asks for it inside the one function that needs it). Every request goes through the autopilot's own
client, so it counts against the app's shared request budget like any other.
"""
from __future__ import annotations

import base64
import json

ROUTE = "/api/internal/siftline/board/%s/column/"
LIST = "?offset=0&limit=999"


class NoLogin(RuntimeError):
    """This workstation has no DataSift login to mint a token from."""


def minted_crm(gap: float = 2.0):
    """The autopilot's client on a token minted from this workstation's login. Refuses when there is
    no login: the uploader client would fall back to the Open API key, and the key cannot make a
    column, so the run would fail part way instead of before its first write."""
    import datasift_api_upload as up
    from agents.autopilot.crm import CRM
    api = up.Api()
    if getattr(api, "api_key", ""):
        raise NoLogin("this workstation carries no DataSift login, so the board's columns cannot be made here (the Open "
                      "API key cannot reach the column route); run from the workstation that holds the login")
    return CRM(api=api, min_gap=gap)


def token_account(crm) -> str:
    """The account a minted token belongs to, from its own claim, so a run can check it writes the
    columns into the same account the plan was read from."""
    tok = str(getattr(getattr(crm, "api", None), "token", "") or "")
    try:
        body = tok.split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get("account") or "")
    except (IndexError, ValueError, UnicodeDecodeError):
        return ""


def list_columns(crm, board_uuid: str) -> list[dict]:
    """[{uuid, title, order}] for every column of the board, in board order."""
    r = crm.call(ROUTE % board_uuid + LIST)
    rows = r if isinstance(r, list) else ((r.get("results") or r.get("data") or []) if isinstance(r, dict) else [])
    out = [{"uuid": str(c.get("uuid") or ""), "title": str(c.get("title") or ""), "order": c.get("order")}
           for c in rows if isinstance(c, dict) and c.get("uuid")]
    return sorted(out, key=lambda c: (c["order"] if isinstance(c["order"], int) else 10 ** 6, c["title"]))


def create_column(crm, board_uuid: str, title: str, order: int) -> dict:
    """One column at the end of the board, at the place `order` gives it."""
    r = crm.call(ROUTE % board_uuid, "POST", {"title": title, "order": int(order)})
    return r if isinstance(r, dict) else {}
