"""Live updates to each caller's phone page (server-sent events).

Twilio's callbacks arrive on worker threads; the page listens on one long request. So events go into a
small per-caller mailbox with a sequence number, and the stream reads whatever is newer than the last one
it sent. A page that reconnects sends Last-Event-ID and gets what it missed (the last 200 per caller).
Nothing here is the system of record: the database is. A page that missed everything still reads the
call's state from /api/call/<id>.
"""
from __future__ import annotations

import threading
import time
from collections import deque

KEEP = 200


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._boxes: dict[str, deque] = {}
        self._cond = threading.Condition(self._lock)

    def publish(self, identity: str, kind: str, data: dict | None = None) -> int:
        if not identity:
            return 0
        with self._cond:
            self._seq += 1
            box = self._boxes.setdefault(identity, deque(maxlen=KEEP))
            box.append((self._seq, kind, dict(data or {}), time.time()))
            self._cond.notify_all()
            return self._seq

    def publish_many(self, identities, kind: str, data: dict | None = None) -> None:
        for ident in set(identities or ()):
            self.publish(ident, kind, data)

    def since(self, identity: str, last: int) -> list[tuple]:
        with self._lock:
            return [e for e in self._boxes.get(identity, ()) if e[0] > last]

    def wait(self, identity: str, last: int, timeout: float) -> list[tuple]:
        """Block (in a worker thread) until something newer than `last` arrives, or the timeout."""
        end = time.time() + timeout
        with self._cond:
            while True:
                got = [e for e in self._boxes.get(identity, ()) if e[0] > last]
                if got:
                    return got
                left = end - time.time()
                if left <= 0:
                    return []
                self._cond.wait(left)

    def latest(self) -> int:
        with self._lock:
            return self._seq


HUB = Hub()


def publish(identity: str, kind: str, data: dict | None = None) -> int:
    return HUB.publish(identity, kind, data)
