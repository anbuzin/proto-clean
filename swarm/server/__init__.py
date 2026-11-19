"""The server: no AI, owns all state. Layers: `http` (routes) -> domain modules -> `store`.

The domain shares handler dependencies in `deps` and the agent write preamble in `writes`.
This package must never import `ai` (invariant I14).
"""
