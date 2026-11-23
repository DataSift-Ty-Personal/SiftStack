"""What a record says: the old notes field plus the message board.

Notes go on the message board since 2026-11-18 (the add-notes route was retired), and DataSift does
not copy them into the record's notes field (read back on seven records on 2026-11-19). Code that
reads a record's words, the death screen, the research brief and the read-backs, reads both.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agents.autopilot import crm as C  # noqa: E402
from agents.research import researcher as R  # noqa: E402


class Fake(C.CRM):
    def __init__(self, notes, board, fail=False):        # no network: record and call are scripted
        self._rec, self._board, self._fail = {"uuid": "u1", "notes": notes}, list(board), fail

    def record(self, uuid):
        return dict(self._rec)

    def call(self, path, method="GET", body=None, **kw):
        if "/message/" in path:
            if self._fail:
                raise RuntimeError("HTTP 500")
            return {"results": [{"message": m} for m in self._board]}
        return {}


def test_said_reads_the_old_notes_field_and_the_board():
    c = Fake("Lien debtor DOE PAT: 1 lien instrument(s) recorded (LEN).",
             ["Foreclosure | Auction: 12/18/2026 | Source: https://example.com/n/1"])
    said = c.said("u1")
    assert "Lien debtor DOE PAT" in said and "Auction: 12/18/2026" in said


def test_said_leaves_out_our_own_research_and_screen_notes():
    c = Fake("", ["[Deep prospecting v5] Owner status: Deceased per skip trace",
                  "Possible death: an obituary names a Pat Doe. Not confirmed.",
                  "Probate | Source: https://example.com/n/2"])
    said = c.said("u1")
    assert "Probate | Source" in said
    assert "Deep prospecting" not in said and "Possible death" not in said


def test_a_board_that_cannot_be_read_counts_as_empty():
    assert Fake("an old note", ["a board note"], fail=True).said("u1") == "an old note"


def test_record_said_falls_back_to_the_notes_field_for_a_client_without_it():
    class Plain:
        pass
    assert C.record_said(Plain(), "u1", {"notes": "abc"}) == "abc"


def test_the_research_brief_reads_the_notice_from_the_board():
    x = {"record": {"notes": "", "owner": {"first_name": "Pat", "last_name": "Doe"},
                    "address": {"county": "Blount"}},
         "messages": [{"message": "Probate | Estate of Lee Doe, deceased | Source: https://example.com/n/3"},
                      {"message": "[Deep prospecting v5] research note"}]}
    look = R.free_look(x, {})
    assert "Estate of Lee Doe" in look["notice_text"] and "Deep prospecting" not in look["notice_text"]
    assert {"deceased", "estate of"} <= set(look["notice_mentions"])
