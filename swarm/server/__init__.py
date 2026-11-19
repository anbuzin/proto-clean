"""The server: no AI, owns all state. Layers: `http` (routes) -> domain modules -> `store`.

Domain modules: `tasks`, `leases`, `steps`, `calls`, `sandboxes`, `processes`, `policy`.
This package must never import `ai` (invariant I14).
"""
