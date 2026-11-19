"""HTTP layer: routing, auth, JSON <-> `swarm.wire`, error replies. No domain logic.

Standard library (`http.server.ThreadingHTTPServer`). For each request:
1. `match(method, path)` against `wire.ROUTES` (method is part of the match; no match: 404).
2. Auth: `owner`, `worker`, `runner` compare the bearer token with `Deps.tokens`
   (`hmac.compare_digest`). `launch` passes the bearer secret to the handler, which checks
   it inside its own write transaction (`processes.authenticate_launch`).
3. Parse the body (POST) or the query (GET) with `route.request`; error: 422.
4. Call `HANDLERS[route.name](deps, request, **path)`; serialize with `route.reply`;
   `None` gives 204. `ApiError` gives `wire.ERROR_STATUS[kind]`; any other exception 500.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Any

from swarm.wire import Route, WireModel

from .config import ServerConfig
from .policy import CommandPolicy
from .store import Store


@dataclass(frozen=True)
class Deps:
    """What every handler gets. Frozen at startup; the only injection seam of the server."""
    store: Store
    config: ServerConfig
    policy: CommandPolicy
    tokens: dict[str, str]          # owner, worker, runner
    started_at: float
    launch_secret: str | None = None   # set per request for `launch` routes


Handler = Callable[..., WireModel | None]   # handler(deps, request, **path_params)


def handlers() -> dict[str, Handler]:
    """Route name -> domain function, e.g. 'commit_step' -> steps.commit_step,
    'health' -> app.health. Startup fails if a name in `wire.ROUTES` has no handler."""
    raise NotImplementedError


def match(method: str, path: str) -> tuple[Route, dict[str, Any]] | None:
    """Route and decoded path parameters (`*_seq` as int)."""
    raise NotImplementedError


def make_http_server(deps: Deps) -> ThreadingHTTPServer:
    raise NotImplementedError
