"""Jira integration package.

Jira is read-only in v1 (Global Constraints): no module under this package
may define a write method, and no POST/PUT/DELETE code path may exist.
`jira.adapter` is a leaf module (imports nothing from the rest of
work_ledger's Jira package or from `work_ledger.connections`) so
`work_ledger.connections` can import it for the capability vocabulary
without creating a cycle (Ruling 13).
"""
