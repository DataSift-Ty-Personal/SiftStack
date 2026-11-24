"""Where the data team's model (engine.py) reads from: the workstation's read-only `opensearch` MCP.

Staff only. The model reads the production property cluster, which reads every account's data, so it runs
through src/opensearch_mcp.py, which refuses to start unless its config says OPENSEARCH_SETTINGS_ALLOW_WRITE
=false; no key lives in this repo. Users run SiftStack's own version (open_api.py) on their Open API key.

The interface is engine.Backend: get(index, id, fields) and search(index, query, sort, fields, limit). It
can only search; there is no write, index or delete call here. Paging is search_after on a sort that ends in
a unique field, 100 rows a call (SearchIndexTool's ceiling), so a large result is read whole up to the limit.
"""
from __future__ import annotations


def _body(query: dict, sort: list, fields, size: int, scored: bool, after, total: bool) -> dict:
    body: dict = {"query": query, "size": size, "track_total_hits": bool(total)}
    if sort:
        body["sort"] = sort
    if fields is not None:
        body["_source"] = fields
    if scored:
        body["track_scores"] = True
    if after is not None:
        body["search_after"] = after
    return body


def _total(resp: dict, fallback: int) -> int:
    t = (resp.get("hits") or {}).get("total")
    if isinstance(t, dict):
        return int(t.get("value") or 0)
    if isinstance(t, int):
        return t
    return fallback


class MCPBackend:
    page_size = 100

    def __init__(self, mcp=None):
        if mcp is None:
            from opensearch_mcp import OpenSearchMCP
            mcp = OpenSearchMCP()
        self.mcp = mcp

    def _search_page(self, index: str, body: dict) -> dict:
        size = body.pop("size", 10)
        return self.mcp.search(body, size, index)

    def search(self, index: str, query: dict, *, sort: list, fields=None, limit: int = 100,
               scored: bool = False) -> tuple[list[dict], int]:
        out: list[dict] = []
        after, total = None, 0
        first = True
        while len(out) < limit:
            size = min(self.page_size, limit - len(out))
            resp = self._search_page(index, _body(query, sort, fields, size, scored, after, first))
            hits = (resp.get("hits") or {}).get("hits") or []
            if first:
                total = _total(resp, len(hits))
                first = False
            out.extend(hits)
            if len(hits) < size or not sort:
                break
            after = hits[-1].get("sort")
            if not after:
                break
        return out, max(total, len(out))

    def get(self, index: str, doc_id, fields=None) -> dict | None:
        hits, _ = self.search(index, {"ids": {"values": [str(doc_id)]}}, sort=[], fields=fields, limit=1)
        return (hits[0].get("_source") or {}) if hits else None

    def close(self) -> None:
        try:
            self.mcp.close()
        except Exception:  # noqa: BLE001
            pass
