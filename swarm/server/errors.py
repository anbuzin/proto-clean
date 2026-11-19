"""The one exception that domain code raises to refuse a request.

The domain names the kind (a `wire.ErrorReply.error` value); only `http.py` maps it to
a status code (`wire.ERROR_STATUS`) and a reply body.
"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind          # unauthorized | not_found | stale | conflict | invalid | busy
        self.detail = detail
