"""The worker: a disposable agent process. It holds only a cache of committed state.

`main` claims agents in a loop. `lease` keeps the lease alive and stops the process when
it cannot. `loop` runs kind `loop` on the `ai` SDK; `external` runs kind `external`
(a coding CLI through `ai.external`). `tools` is the tool registry; `models` is the
model seam (a `ModelFactory`), so a test can give `ai.testing.FakeModel`.
"""
