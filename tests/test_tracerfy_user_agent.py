"""The autopilot's Tracerfy client must name its User-Agent on every request.

Tracerfy's Cloudflare refuses urllib's default "Python-urllib/3.x" agent with HTTP 403 "error code:
1010", so a retrace that sent the default bought nothing and wrote nothing (2026-11-20). Zero network:
urlopen is replaced and every request it is handed is recorded.
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.autopilot import sources  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_every_tracerfy_request_names_its_agent(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        if req.full_url.startswith(sources.TRACERFY_TRACE_URL):
            return _Resp(json.dumps({"queue_id": 7}).encode())
        return _Resp(json.dumps({"status": "completed", "records": [
            {"first_name": "Sam", "last_name": "Sample", "address": "1 Test Way", "mobile_1": "8655550100"}]}).encode())

    monkeypatch.setattr(sources.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    subj = sources.Subject(record_uuid="r1", owner_uuid="o1", kind="person", first="Sam", last="Sample",
                           street="1 Test Way", city="Knoxville", state="TN", zip="37900",
                           mail_street="1 Test Way", mail_city="Knoxville", mail_state="TN", mail_zip="37900")
    found, billed = sources.Tracerfy(key="k", poll_s=0).trace([subj])
    assert found == {"r1": [{"number": "8655550100", "type": "MOBILE"}]}
    assert billed == 0.02
    assert len(seen) == 2
    for req in seen:
        ua = req.get_header("User-agent") or ""
        assert ua and not ua.startswith("Python-urllib"), ua
