"""Read-only client for the `opensearch` MCP server, driven by its user-scope config in ~/.claude.json.

Scripts get the same guarded access Claude has, not a second door: the server must be configured with
OPENSEARCH_SETTINGS_ALLOW_WRITE=false (the client refuses to start otherwise), SearchIndexTool caps every
call at 100 rows, and OPENSEARCH_QUERY_TIMEOUT cancels a runaway query on the production cluster. The
credentials stay in the Claude config; nothing in this repo holds one.

Two contracts learned live (2026-11-05):
  * GenericOpenSearchApiTool drops the body on a GET, so a search must go through SearchIndexTool, which
    passes the whole body (sort, search_after, _source, aggs) and only overrides `size`.
  * Past 100 rows, page with search_after on a sort that ends in a unique field (dataflik_id).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


def first_json(txt: str):
    """The first JSON object or array in a tool's text response."""
    for i, ch in enumerate(txt or ""):
        if ch in "{[":
            try:
                return json.JSONDecoder().raw_decode(txt[i:])[0]
            except ValueError:
                continue
    return None


class OpenSearchMCP:
    def __init__(self, server: str = "opensearch"):
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"][server]
        if cfg.get("env", {}).get("OPENSEARCH_SETTINGS_ALLOW_WRITE") != "false":
            raise SystemExit("REFUSING: the %s MCP server is not configured read-only" % server)
        cmd = [shutil.which(cfg["command"]) or cfg["command"], *cfg.get("args", [])]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, **cfg["env"]}, text=True, encoding="utf-8", errors="replace")
        self.q, self.err, self.n = queue.Queue(), [], 0
        threading.Thread(target=lambda: [self.q.put(x) for x in self.p.stdout], daemon=True).start()
        threading.Thread(target=lambda: [self.err.append(x) for x in self.p.stderr], daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "siftstack", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method: str, params=None, timeout: float = 300):
        self.n += 1
        i = self.n
        self._send({"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                if self.p.poll() is not None:
                    break
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == i:
                return msg
        raise RuntimeError("no response to %s; server stderr tail:\n%s" % (method, "".join(self.err[-15:])))

    def tool(self, name: str, args: dict) -> str:
        resp = self._rpc("tools/call", {"name": name, "arguments": args})
        r = resp.get("result") or {}
        txt = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        return txt or json.dumps(resp.get("error") or r)

    def search(self, body: dict, size: int = 0, index: str = "properties", tries: int = 4) -> dict:
        for attempt in range(tries):
            txt = self.tool("SearchIndexTool", {"index": index, "query_dsl": body, "size": size})
            js = first_json(txt)
            if isinstance(js, dict) and "hits" in js:
                return js
            if attempt < tries - 1 and any(s in txt for s in ("429", "503", "timed out", "Timeout")):
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError("search failed: %s" % txt[:400])

    def count(self, query: dict, index: str = "properties") -> int:
        return self.search({"query": query, "track_total_hits": True}, 0, index)["hits"]["total"]["value"]

    def page(self, query: dict, sort: list, fields: list, limit: int, index: str = "properties"):
        """Rows in sort order via search_after, 100 at a time, up to `limit`."""
        after, got = None, 0
        while got < limit:
            body = {"query": query, "sort": sort, "_source": fields, "track_total_hits": False}
            if after:
                body["search_after"] = after
            hits = self.search(body, 100, index)["hits"]["hits"]
            if not hits:
                return
            for h in hits:
                yield h["_source"]
                got += 1
                if got >= limit:
                    return
            after = hits[-1]["sort"]

    def close(self) -> None:
        self.p.stdin.close()
        try:
            self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.p.kill()
