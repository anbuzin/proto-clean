"""Disposable worker processes hold a cache of committed agent state.

`loop` runs model agents and `external` runs Fx turns. Both use `steps` for
writes that can release the lease. `lease` stops the worker if renewal fails.
"""
