"""Zero-network checks for smarter_contact_setup.TokenSession (the build's fallback when the browser is signed out).

    python tests/test_smarter_contact_setup.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smarter_contact_setup as S  # noqa: E402

FAILS: list[str] = []
PASSED = [0]


def check(name: str, ok: bool, got=None) -> None:
    if ok:
        PASSED[0] += 1
    else:
        FAILS.append(f"{name}: got {got!r}")


class FakeSC:
    GQL = "https://example.invalid/gql"

    def __init__(self):
        self.tokens = 0

    def _id_token(self):
        self.tokens += 1
        return "id-token"


def session_with(responder):
    s = S.TokenSession.__new__(S.TokenSession)
    s.sc, s.headers = FakeSC(), None
    sent = []

    def urlopen(req, timeout=0):
        sent.append({"url": req.full_url, "auth": req.get_header("Authorization"),
                     "body": json.loads(req.data)})
        return responder(req)

    S.urllib.request.urlopen = urlopen
    return s, sent


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main() -> int:
    real = S.urllib.request.urlopen
    try:
        # 1. A normal response comes back as the raw envelope, data and all.
        s, sent = session_with(lambda req: Resp(json.dumps({"data": {"findLabels": []}}).encode()))
        got = asyncio.run(s.call("query { findLabels { id } }", {"a": 1}, "FindLabels"))
        check("200 returns the raw envelope", got == {"data": {"findLabels": []}}, got)
        check("bearer comes from the agent login", sent[0]["auth"] == "Bearer id-token", sent[0]["auth"])
        check("operationName is sent when given", sent[0]["body"].get("operationName") == "FindLabels", sent[0]["body"])
        check("variables are sent", sent[0]["body"]["variables"] == {"a": 1}, sent[0]["body"])

        # 2. THE RULE: a validation error is HTTP 400 with a JSON body, and the build's
        #    dry-run probe must READ it. Raising here would break every dry run.
        body = {"errors": [{"message": f'Field "{S.DRY_FIELD}" is not defined by type "X".'}]}

        def bad(req):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))

        s, _ = session_with(bad)
        got = asyncio.run(s.call("mutation { x }"))
        check("HTTP 400 JSON is returned as data, not raised", got == body, got)
        errs = asyncio.run(S._server_validates(s, "mutation M($input: X!) { m(input: $input) { id } }", {"input": {}}))
        check("the validation probe reads a 400 as 'no other problems'", errs == [], errs)

        # 3. A non-JSON error body still comes back as an errors envelope the build can print.
        def html(req):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>bad gateway</html>"))

        s, _ = session_with(html)
        got = asyncio.run(s.call("query { x }"))
        check("non-JSON error becomes an errors envelope", "HTTP 502" in got["errors"][0]["message"], got)

        # 4. No operationName key when none is given (the browser Session behaves the same).
        s, sent = session_with(lambda req: Resp(b'{"data": {}}'))
        asyncio.run(s.call("query { x }"))
        check("no operationName when not given", "operationName" not in sent[0]["body"], sent[0]["body"])
    finally:
        S.urllib.request.urlopen = real

    if FAILS:
        print(f"test_smarter_contact_setup: {len(FAILS)} FAILED")
        for f in FAILS:
            print("  " + f)
        return 1
    print(f"test_smarter_contact_setup: {PASSED[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
