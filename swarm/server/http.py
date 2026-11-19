"""HTTP routing, bearer authentication, and wire-model validation."""
from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import ValidationError
from swarm import wire
from swarm.wire import Route, WireModel
from .config import ServerConfig
from .errors import ApiError
from .policy import CommandPolicy
from .store import Store


@dataclass(frozen=True)
class Deps:
    """Dependencies shared by route handlers."""
    store: Store
    config: ServerConfig
    policy: CommandPolicy
    tokens: dict[str, str]
    started_at: float
    launch_secret: str | None = None


Handler = Callable[..., WireModel | None]


def handlers() -> dict[str, Handler]:
    """Map every wire route to its domain handler."""
    from . import app, calls, leases, processes, steps, tasks
    modules = (app, calls, leases, processes, steps, tasks)
    result = {}
    for route in wire.ROUTES:
        handler = next((getattr(module, route.name) for module in modules if hasattr(module, route.name)), None)
        if handler is None:
            raise RuntimeError(f"missing handler: {route.name}")
        result[route.name] = handler
    return result


def match(method: str, path: str) -> tuple[Route, dict[str, Any]] | None:
    """Match a method and path, decoding path parameters."""
    for route in wire.ROUTES:
        if method != route.method:
            continue
        parts = re.split(r"(\{[a-z_]+\})", route.path)
        pattern = "".join(f"(?P<{part[1:-1]}>[^/]+)" if part.startswith("{") else re.escape(part) for part in parts)
        found = re.fullmatch(pattern, path)
        if found:
            params: dict[str, Any] = {key: unquote(value) for key, value in found.groupdict().items()}
            for key, value in params.items():
                if key.endswith("_seq"):
                    try:
                        params[key] = int(value)
                    except ValueError as exc:
                        raise ApiError("invalid", f"invalid {key}") from exc
            return route, params
    return None


def make_http_server(deps: Deps) -> ThreadingHTTPServer:
    """Create a threaded server with the configured dependencies."""
    routes = handlers()

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.dispatch()

        def do_POST(self) -> None:
            self.dispatch()

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST
        do_HEAD = do_POST

        def dispatch(self) -> None:
            try:
                parsed = urlsplit(self.path)
                matched = match(self.command, parsed.path)
                if matched is None:
                    raise ApiError("not_found")
                route, path = matched
                auth = self.headers.get("Authorization", "")
                secret = auth[7:] if auth.startswith("Bearer ") else None
                if route.auth not in ("none", "launch"):
                    expected = deps.tokens[route.auth]
                    if secret is None or not hmac.compare_digest(secret, expected):
                        raise ApiError("unauthorized")
                request_deps = replace(deps, launch_secret=secret) if route.auth == "launch" else deps
                request = None
                if route.request is not None:
                    try:
                        if self.command == "GET":
                            pairs = parse_qsl(parsed.query, keep_blank_values=True)
                            if len(dict(pairs)) != len(pairs):
                                raise ApiError("invalid", "duplicate query parameter")
                            request = route.request.model_validate(dict(pairs))
                        else:
                            length = int(self.headers.get("Content-Length", "0"))
                            if length < 0 or length > 16 * 1024 * 1024:
                                raise ApiError("invalid", "invalid body length")
                            request = route.request.model_validate_json(self.rfile.read(length))
                    except (ValidationError, ValueError, UnicodeError) as exc:
                        raise ApiError("invalid", str(exc)) from exc
                result = routes[route.name](request_deps, request, **path)
                if result is None:
                    self.send_response(204)
                    self.end_headers()
                else:
                    payload = route.reply.model_validate(result).model_dump_json().encode()
                    self.respond(200, payload)
            except ApiError as exc:
                self.error_reply(exc.kind, exc.detail)
            except Exception:
                logging.exception("HTTP request failed")
                self.error_reply("internal", "internal server error")

        def error_reply(self, kind: str, detail: str) -> None:
            self.respond(wire.ERROR_STATUS.get(kind, 500), wire.ErrorReply(error=kind, detail=detail).model_dump_json().encode())

        def respond(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    return ThreadingHTTPServer((deps.config.host, deps.config.port or 0), RequestHandler)
