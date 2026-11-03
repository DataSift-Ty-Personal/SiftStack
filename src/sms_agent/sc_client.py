"""Smarter Contact's internal app API, used with Ty's own login.

Smarter Contact has no public API on this account (its developer platform stays
switched off), so replies and labels go through the same GraphQL the web app
uses, authenticated the way the web app is: a Firebase ID token (1 hour) minted
from the account's refresh token. Verified 2026-10-27 that plain HTTP from a
server can mint the token and call the API, so no browser runs on Fly.

Ty approved this route knowing the trade-off: it is not an official
integration, the calls can change without notice, and a password change or
"sign out everywhere" kills the refresh token. Every failure raises SCError so
the worker can alert the channel instead of failing quietly.

Contracts (read from the app bundle and Apollo validation, 2026-10-27):
  SendMessage(input: {contactId, content})  replies on the thread's own number
  AddLabelsToContacts(input: {ids, labelIds})
  FindConversationMessages(contactId, pagination) -> items {direction, createdAt,
      content, campaign{id}, template{id}, workflow{name}}
  FindContacts(filter: {status: ACTIVE, fullSearch: <10 digits>})  phone lookup;
      `status` is required on ContactFilter and `phone`/`search` do not exist
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger(__name__)

GQL = "https://api.smartercontact.com/gql"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={key}"

_token = {"id": "", "exp": 0.0, "refresh": ""}
_labels: dict[str, str] = {}


class SCError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.SC_FIREBASE_API_KEY and config.SC_REFRESH_TOKEN)


def _post(url: str, data: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SCError(f"HTTP {e.code} from {url.split('?')[0]}: {e.read()[:200]!r}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise SCError(f"network error to {url.split('?')[0]}: {e}") from None


def _id_token() -> str:
    if _token["id"] and time.time() < _token["exp"] - 300:
        return _token["id"]
    if not configured():
        raise SCError("Smarter Contact login not configured (SMARTERCONTACT_REFRESH_TOKEN / _FIREBASE_API_KEY)")
    refresh = _token["refresh"] or config.SC_REFRESH_TOKEN
    res = _post(TOKEN_URL.format(key=config.SC_FIREBASE_API_KEY),
                urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode(),
                {"content-type": "application/x-www-form-urlencoded"})
    if not res.get("id_token"):
        raise SCError(f"login renewal refused: {json.dumps(res)[:200]}")
    _token.update(id=res["id_token"], exp=time.time() + int(res.get("expires_in", 3600)),
                  refresh=res.get("refresh_token") or refresh)
    return _token["id"]


def gql(query: str, variables: dict | None = None) -> dict:
    res = _post(GQL, json.dumps({"query": query, "variables": variables or {}}).encode(),
                {"content-type": "application/json", "authorization": f"Bearer {_id_token()}"})
    if res.get("errors"):
        raise SCError(json.dumps(res["errors"])[:300])
    return res.get("data") or {}


def send_message(contact_id: str, content: str) -> str:
    data = gql("mutation SendMessage($input: SendMessageInput!) { sendMessage(input: $input) { id } }",
               {"input": {"contactId": contact_id, "content": content}})
    return ((data.get("sendMessage") or {}).get("id")) or ""


def direction(message: dict) -> str:
    """'in', 'out' or '' for a Smarter Contact message.

    The enum's spelling was not observable before the first live reply (the
    account had no conversations), so this reads either INCOMING/INBOUND or
    OUTGOING/OUTBOUND, and anything else comes back '' so callers fail closed.
    """
    d = str(message.get("direction") or "").upper()
    return "in" if d.startswith("IN") else "out" if d.startswith("OUT") else ""


def thread(contact_id: str, limit: int = 12) -> list[dict]:
    """The latest messages on a contact's thread, oldest first.

    Each row is {id, dir, body, created_at, manual}; `manual` is True for an
    outbound text no template or workflow sent, which is a person typing in
    the inbox (or one of our own auto replies). NOT the campaign field: once a
    thread belongs to a campaign, Smarter Contact stamps that campaign on every
    later message, typed ones included (read live 2026-10-28). Campaign touches
    are the ones that also carry a template.
    """
    data = gql("query M($contactId: String!, $pagination: Pagination) { findConversationMessages("
               "contactId: $contactId, pagination: $pagination) { items { id direction status createdAt "
               "content campaign { id } template { id } workflow { name } } } }",
               {"contactId": contact_id, "pagination": {"limit": limit}})
    items = ((data.get("findConversationMessages") or {}).get("items")) or []
    rows = [{"id": m.get("id"), "dir": direction(m), "body": m.get("content") or "",
             "created_at": m.get("createdAt") or "", "raw_direction": m.get("direction"),
             "manual": not (m.get("template") or m.get("workflow"))}
            for m in items]
    return sorted(rows, key=lambda r: r["created_at"])


_apn_field = {"id": ""}


def contact_record_uuid(contact_id: str) -> str:
    """The DataSift record this contact was pushed from ('' when unknown).

    The DataSift integration writes the RECORD UUID into the contact's APN
    field. It is the record that was actually texted, which matters because a
    phone can sit on several records and the phone map holds whichever one an
    older campaign saw first: 9 of the first 74 replies resolved elsewhere.
    """
    if not contact_id:
        return ""
    if not _apn_field["id"]:
        fields = gql("query { findFields { id key } }").get("findFields") or []
        _apn_field["id"] = next((f["id"] for f in fields if f.get("key") == "ASSESSOR_PARCEL_NUMBER"), "")
    data = gql("query G($id: String!) { getContact(id: $id) { attrs { fieldId value } } }", {"id": contact_id})
    attrs = (data.get("getContact") or {}).get("attrs") or []
    value = next(((a.get("value") or "").strip() for a in attrs if a.get("fieldId") == _apn_field["id"]), "")
    return value if len(value) == 36 and value.count("-") == 4 else ""


_pending = {"at": 0.0, "rows": []}


def pending_followups() -> list[dict]:
    """Follow-ups not finished sending, as [{id, groups}] (cached 10 minutes).

    A follow-up's recipients are a group Smarter Contact snapshots when the
    follow-up is CREATED ("filtered_from_campaigns_<date>"), so anyone who
    replies after that would still get the next touch unless taken out of it.
    """
    if time.time() - _pending["at"] > 600:
        items = gql("query F($p: Pagination) { findCampaigns(pagination: $p) { items { id state { status } "
                    "parentCampaign { ids } groups { id } } } }", {"p": {"limit": 100}}).get(
                        "findCampaigns", {}).get("items") or []
        # Every campaign not finished sending: follow-ups AND the standard
        # Touch 2+ campaigns built on audited groups. A replier is in at most a
        # few of these, and removing someone from a group they are not in is a no-op.
        _pending["rows"] = [{"id": c["id"], "groups": [g["id"] for g in c.get("groups") or []]}
                            for c in items
                            if (c.get("state") or {}).get("status") not in ("ENDED", "DELETED", "COMPLETED")]
        _pending["at"] = time.time()
    return [dict(r) for r in _pending["rows"]]


def remove_from_group(group_id: str, contact_ids: list[str]) -> bool:
    """Take these contacts out of one group. Refuses an empty list: the
    mutation's `ids` is optional and what it does without one is untested."""
    ids = [c for c in contact_ids if c]
    if not (group_id and ids):
        raise SCError("remove_from_group needs a group and at least one contact id")
    data = gql("mutation R($input: RemoveGroupFromContactsInput!) { removeGroupFromContacts(input: $input) "
               "{ success } }", {"input": {"groupId": group_id, "ids": ids}})
    return bool((data.get("removeGroupFromContacts") or {}).get("success"))


def add_contact_dnc(contact_id: str) -> str:
    """Put a contact on Smarter Contact's own DNC suppression list.

    The platform blocks only the literal STOP keyword on its own, so "take me
    off your list" or "wrong number" would otherwise still get the next touch
    of a campaign that is already scheduled. A suppressed number is texted by
    no campaign. Returns the suppression source, or "already" if it was there.
    """
    try:
        row = gql("mutation A($input: AddContactToDNCInput!) { addContactToDNC(input: $input) { id source } }",
                  {"input": {"contactId": contact_id}}).get("addContactToDNC") or {}
    except SCError as exc:
        if "already" in str(exc).lower() or "exist" in str(exc).lower():
            return "already"
        raise
    return str(row.get("source") or "DNC")


def remove_from_campaign(contact_id: str, campaign_id: str) -> bool:
    """For a campaign already SENDING: take the contact out of it. A draft or
    scheduled follow-up ignores this; use remove_from_group on its group."""
    try:
        data = gql("mutation R($contactId: String!, $campaignId: String!) { removeContactFromCampaign("
                   "contactId: $contactId, campaignId: $campaignId) { success } }",
                   {"contactId": contact_id, "campaignId": campaign_id})
    except SCError:
        return False   # not in that campaign
    return bool((data.get("removeContactFromCampaign") or {}).get("success"))


def contact_id_for(phone: str) -> str:
    """The contact id for a phone number ('' when Smarter Contact has none)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())[-10:]
    if len(digits) != 10:
        return ""
    data = gql("query F($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) "
               "{ items { id phone } } }", {"f": {"status": "ACTIVE", "fullSearch": digits}, "p": {"limit": 5}})
    for c in ((data.get("findContacts") or {}).get("items")) or []:
        if "".join(ch for ch in str(c.get("phone")) if ch.isdigit())[-10:] == digits:
            return c["id"]
    return ""


def _label_ids(titles: list[str]) -> list[str]:
    if not all(t in _labels for t in titles):
        data = gql("query L($pagination: CursorPagination) { findLabels(pagination: $pagination) "
                   "{ items { id title } } }", {"pagination": {"limit": 200}})
        v = data.get("findLabels") or {}
        for lab in (v.get("items") if isinstance(v, dict) else v) or []:
            _labels[lab["title"]] = lab["id"]
    missing = [t for t in titles if t not in _labels]
    if missing:
        raise SCError(f"labels not found in Smarter Contact: {missing}")
    return [_labels[t] for t in titles]


def add_labels(contact_ids: list[str], titles: list[str]) -> None:
    data = gql("mutation A($input: AddLabelsToContactsInput!) { addLabelsToContacts(input: $input) { success } }",
               {"input": {"ids": contact_ids, "labelIds": _label_ids(titles)}})
    if not (data.get("addLabelsToContacts") or {}).get("success"):
        raise SCError(f"label refused: {json.dumps(data)[:200]}")


def health() -> tuple[bool, str]:
    try:
        data = gql("query { getContactsCounters { activeContacts } }")
        return True, f"ok, {((data.get('getContactsCounters') or {}).get('activeContacts'))} active contacts"
    except SCError as e:
        return False, str(e)
