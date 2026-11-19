"""A model factory owns the provider and transport for one claim."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import ai
import httpx2
from ai.providers.ai_gateway import GatewayProvider
from ai.types.messages import Message

from swarm.wire import TaskSpec

# The context yields the model and closes its provider and HTTP client on exit.
ModelFactory = Callable[[TaskSpec], AbstractAsyncContextManager[ai.Model]]


def gateway_model(api_key: str) -> ModelFactory:
    """Do not use a proxy or retry transport: a retry needs a new admission (I11)."""
    @asynccontextmanager
    async def open_model(spec: TaskSpec) -> AsyncIterator[ai.Model]:
        if not api_key:
            raise ValueError("AI_GATEWAY_API_KEY is required for a Gateway model")
        transport = httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
        async with httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
            provider = GatewayProvider(api_key_value=api_key, base_url_env=None)
            provider._set_http_client(client)
            try:
                yield ai.Model(id=spec.model, provider=provider)
            finally:
                await provider.aclose()

    return open_model


def estimate_tokens(messages: list[Message], tools: list[ai.AgentTool], max_output_tokens: int) -> int:
    """UTF-8 bytes of the JSON of messages and tool schemas / 4, plus `max_output_tokens`.
    A heuristic: overshoot is caught at commit (I11)."""
    size = sum(len(message.model_dump_json().encode()) for message in messages)
    size += sum(len(tool.tool.model_dump_json().encode()) for tool in tools)
    return (size + 3) // 4 + max_output_tokens
