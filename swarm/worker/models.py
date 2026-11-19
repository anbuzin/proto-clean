"""The model seam. The worker gets its model from a `ModelFactory`; the product has one
factory (`gateway_model`). A test passes its own factory, for example one that returns
`ai.testing.FakeModel`, with `--model-factory tests.fakes:fake_model`. No scenario code
is in the product.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import ai

from swarm.wire import TaskSpec

# A factory returns an async context manager that yields the model and closes its
# provider client on exit.
ModelFactory = Callable[[TaskSpec], AbstractAsyncContextManager[ai.Model]]


def gateway_model(spec: TaskSpec) -> AbstractAsyncContextManager[ai.Model]:
    """`ai.Model(id=spec.model, provider=GatewayProvider(...))` with the key from
    `AI_GATEWAY_API_KEY` in the worker environment. No provider retries (the loop owns
    retries); no proxy from the environment."""
    raise NotImplementedError


def load_factory(ref: str) -> ModelFactory:
    """Import `module:function` and return it."""
    raise NotImplementedError


def estimate_tokens(messages: list[ai.types.messages.Message], tools: list[ai.Tool], output_cap: int) -> int:
    """Input estimate (UTF-8 bytes of the JSON / 4) plus `output_cap`. A heuristic, not a cap."""
    raise NotImplementedError
