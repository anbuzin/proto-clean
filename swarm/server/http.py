"""HTTP layer: routing, auth, JSON <-> `swarm.wire` models, error replies. No domain logic.

Built on the standard library (`http.server.ThreadingHTTPServer`). The route table is
`wire.ROUTES`; this module maps each route name to one handler in the domain modules.
For each request: match the path template, check the bearer token for `route.auth`,
validate the body (POST) or query (GET) with `route.request`, call the handler, and
serialize the reply with `route.reply`. A handler reply of `None` gives `204`.
`ApiError` gives its `wire.ERROR_STATUS` code; any other exception gives `500`.

Auth: `owner`, `worker`, `runner` tokens come from `tokens.json`; `launch` checks the
launch secret against `launches.secret_hash` (done by `processes`).
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import Any

from swarm.wire import Route, WireModel

from .store import Store

Handler = Callable[..., WireModel | None]   # handler(store, auth, request, **path_params)


def handlers() -> dict[str, Handler]:
    """Route name -> domain function, e.g. 'commit_step' -> steps.commit_step.
    Must cover every name in `wire.ROUTES` (checked at startup)."""
    raise NotImplementedError


def match(path: str) -> tuple[Route, dict[str, str]] | None:
    """Find the route whose template matches the URL path; return it with path params."""
    raise NotImplementedError


def make_http_server(store: Store, tokens: dict[str, str], host: str, port: int,
                     context: Any) -> ThreadingHTTPServer:
    """Build the server. `context` is the `app.Server` (config, policy) given to handlers."""
    raise NotImplementedError
