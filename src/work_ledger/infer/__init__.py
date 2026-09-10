"""Inference layer: Stage A (deterministic) and Stage B (LLM, best-effort).

`capture.sessions.run_stage_a` / `run_stage_b` import
`work_ledger.infer.stage_a` / `stage_b` lazily *by name* so the capture
layer keeps working before either module exists. Nothing in this package
may import `work_ledger.capture.sessions` at module scope in a way that
would close that loop -- `stage_a` imports it for `sessions.get` only,
which is safe because `sessions` does its own import of this package
inside a function body.
"""
