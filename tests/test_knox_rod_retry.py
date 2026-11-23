"""A dropped connection on a Register of Deeds search is tried again.

Zero network. On 2026-11-20 one "Connection reset by peer" on a release search
failed the whole liens stage for the day.
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_rod  # noqa: E402


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return '{"recordsTotal": 1, "aaData": [["x"]]}'


def test_a_reset_connection_is_tried_again(monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("Connection reset by peer")
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    out = knox_rod._post({}, {}, wait=0)
    assert out["recordsTotal"] == 1
    assert len(calls) == 2


def test_it_gives_up_after_the_last_try(monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.ConnectionError):
        knox_rod._post({}, {}, wait=0, tries=3)
    assert len(calls) == 3


def test_an_http_error_is_not_retried(monkeypatch):
    calls = []

    class _Bad(_Resp):
        def raise_for_status(self):
            raise requests.HTTPError("500")

    def fake_post(*a, **k):
        calls.append(1)
        return _Bad()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.HTTPError):
        knox_rod._post({}, {}, wait=0)
    assert len(calls) == 1
