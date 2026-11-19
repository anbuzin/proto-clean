"""The model seam: the worker gets its model from a `ModelFactory` given at construction.

The product factory is `gateway_model`. A test gives a factory that returns
`ai.testing.FakeModel`; no scenario code is in the product.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import ai

from swarm.wire import TaskSpec

# The context yields the model and closes its provider and HTTP client on exit.
ModelFactory = Callable[[TaskSpec], AbstractAsyncContextManager[ai.Model]]


def gateway_model(api_key: str) -> ModelFactory:
    """A factory for `ai.Model(id=spec.model, provider=GatewayProvider(...))`.
    Import `ai.providers.ai_gateway.GatewayProvider`; pass `api_key_value=api_key`,
    `base_url_env=None`; inject an `httpx2.AsyncClient(transport=AsyncHTTPTransport(
    retries=0, trust_env=False), trust_env=False, follow_redirects=False)` through
    `provider._set_http_client` (as `proto/agent/live.py` does: no proxy from the
    environment, no transport retry). The context closes the provider, then the client."""
    raise NotImplementedError


def estimate_tokens(messages: list[ai.types.messages.Message], tools: list[ai.Tool], max_output_tokens: int) -> int:
    """UTF-8 bytes of the JSON of messages and tool schemas / 4, plus `max_output_tokens`.
    A heuristic: overshoot is caught at commit (I11)."""
    raise NotImplementedError
