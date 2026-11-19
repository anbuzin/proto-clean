"""The client library: one typed HTTP client for every caller of the server.

`OwnerClient` (owner CLI, tests), `AgentClient` (worker), `RunnerClient` (runner),
`LaunchClient` (shim). All are thin facades over `HttpClient`, built from `wire.ROUTES`.
Standard library only (`urllib.request`); it must not import `ai`.
"""

from .api import AgentClient, HttpClient, LaunchClient, OwnerClient, Rejected, RunnerClient, Unreachable

__all__ = ["AgentClient", "HttpClient", "LaunchClient", "OwnerClient", "Rejected", "RunnerClient", "Unreachable"]
