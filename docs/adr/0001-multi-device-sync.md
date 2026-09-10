# ADR 0001 — Multi-device synchronization

- **Status:** Accepted (deferred implementation)
- **Date:** 2026-08-30
- **Deciders:** Work Ledger v1 architecture review
- **Spec references:** SPEC §1, §2.2 (non-goal), §15, §18, §23 ("version one is single-device")
- **Design reference:** [`../DESIGN.md`](../DESIGN.md)

## Context

Work Ledger v1 is explicitly single-device and single-user. Synchronizing the ledger across a laptop, a desktop, and a work machine is a plausible v2 request: the user works with Claude Code on more than one machine, and a work history that is only half-present on each is worth much less than one that is whole.

The spec anticipates this (§18): *"Although synchronization is deferred, design records with global IDs, origin device, update timestamps, and append-only transitions so a future server-backed sync layer does not require a destructive redesign."*

Three forces shape the decision:

1. **Building sync now is expensive and premature.** It brings conflict resolution, transport, authentication, and a server or hosted service — all of which are v1 non-goals (SPEC §2.2), and none of which can be designed well before real single-device usage exists.
2. **Retrofitting sync onto a schema that was not built for it is destructive.** Integer autoincrement primary keys, last-write-wins mutable rows, and materialized derived state make a later merge either lossy or impossible. That cost is paid at v2 time and paid badly.
3. **The naive workaround is dangerous.** Putting `work-ledger.sqlite` in Dropbox, iCloud Drive, or another synchronized filesystem corrupts SQLite databases — two machines with WAL journals over a file-sync layer is a known data-loss pattern.

## Decision

**Defer synchronization to a future version, but make v1's persistence model sync-ready by construction, and actively prevent the dangerous workaround.**

Specifically, v1 commits to the following invariants, which a future sync layer can build on without a schema rewrite:

1. **Globally unique identifiers.** Every row's primary key is a UUIDv7 (`work_ledger/ids.py`), generated locally, never a sequence. Two devices can create rows forever without collision. Human-facing `LOCAL-NNNN` display IDs come from a per-device counter and are treated as *labels*, not identities — a future sync layer may re-label on conflict without touching any foreign key.
2. **Origin device on every synchronizable record.** `origin_device_id` is `NOT NULL` on every structured and append-only table, and the `devices` table holds a stable per-machine `device_key`. Provenance is answerable for every row before any sync exists.
3. **UTC ISO-8601 `created_at` / `updated_at` on every mutable record.** Ordering across devices is possible without relying on a shared clock authority for storage (see Consequences for what this does *not* solve).
4. **Append-only history for anything that carries meaning over time.** `source_events`, `remaining_item_transitions`, `corrections`, and `capture_checkpoints` are never updated and never hard-deleted. Merging two devices' append-only tables is a union keyed by `id` / `idempotency_key` — an operation with no conflict semantics to design.
5. **Idempotency keys on captured events and derived activities.** `source_events.idempotency_key` and `activities.idempotency_key` are unique. Re-delivery of the same evidence from any source, on any device, at any time, is a no-op. This is the same property sync needs for at-least-once transport.
6. **Soft deletion for structured records.** `deleted_at` rather than `DELETE`, so a deletion is a replicable fact rather than an absence that a merge would silently undo. Every uniqueness constraint that must survive deletion is a partial unique index with `WHERE deleted_at IS NULL`.
7. **Derived state is a rebuildable projection.** Activities, remaining items, decisions, and contradictions are regenerable from `source_events` (SPEC §16.2, `worklog reprocess`), and `summary_cache` is explicitly a cache. A future sync layer can therefore synchronize *evidence and corrections* — the authoritative, append-only, conflict-free layer — and let each device rebuild its own interpretation, which is dramatically simpler than merging interpretations.
8. **No sync-hostile storage.** `worklog doctor` detects a data directory inside Dropbox, iCloud Drive, Google Drive, OneDrive, or a network mount and reports a hard warning, with the SQLite-over-file-sync corruption risk stated in RECOVERY.md.

v1 ships **no** transport, **no** server, **no** merge algorithm, and **no** conflict UI.

## Considered alternatives

| Alternative | Why not now |
|---|---|
| **A. File-synced SQLite (Dropbox/iCloud)** | Corrupts WAL databases under concurrent access; silent data loss. Actively warned against (invariant 8). |
| **B. Litestream / SQLite replication to object storage** | Single-writer replication only; gives backup and restore, not multi-device authorship. A reasonable *backup* story, orthogonal to this ADR. |
| **C. CRDT-based local-first sync (e.g. per-field last-writer-wins registers, or an operation log)** | The strongest long-term answer, and invariants 1–7 are chosen to keep it reachable. Too much machinery for v1: needs a transport, a server or peer discovery, an identity model, and a conflict story for corrections. |
| **D. Server-backed sync with a hosted API** | Hosted dashboards and multi-user access are explicit v1 non-goals (SPEC §2.2); it also turns a local, private work history into a service that stores client-identifying data. |
| **E. Export/import as manual sync** | Already shipped in v1 (`worklog export` / `worklog import`, idempotent upsert by global `id`, D12). It is the sanctioned stopgap: a user can move history between machines today, accepting that it is manual and coarse. |

## Consequences

**Positive**

- v2 sync can be implemented as *replication of two append-only tables plus a soft-deletable entity set*, with per-device rebuild of interpretation — no schema migration that loses data.
- Export/import (alternative E) already gives a manual multi-device path with the correct merge semantics (upsert by global `id`), so the deferral is not a dead end for users.
- The invariants pay for themselves in v1 anyway: idempotency makes hook re-delivery safe (SPEC §20.2), append-only history makes corrections auditable (SPEC §5.5), and rebuildable projections make Stage B failures recoverable (SPEC §19).

**Negative / accepted costs**

- Every table carries `origin_device_id`, UUID keys, and timestamps that v1 alone does not strictly need — slightly larger rows and slightly more code.
- Partial unique indexes (`WHERE deleted_at IS NULL`) are subtler than plain unique constraints and must be tested deliberately.
- **Clock skew is not solved.** `updated_at` orders events adequately for a single device, but a future sync layer will need a logical clock (a per-device Lamport counter or an HLC column) to order concurrent edits. Adding one is an additive migration — a new nullable column plus a backfill — which is why it is acceptable to defer.
- **Correction merge semantics are undesigned.** Two devices correcting the same activity differently is a real conflict, not a union. The append-only `corrections` table preserves both intentions so a future merge can present them, but choosing a winner is deferred work.

## Follow-up work for a future sync version

1. Add a logical clock column (`lamport_seq` or an HLC string) to synchronizable tables; additive migration, backfill from `updated_at`.
2. Define a sync protocol over the append-only tables first (`source_events`, `remaining_item_transitions`, `corrections`), which need only union semantics.
3. Define entity merge rules for `clients`, `projects`, `work_items`, `aliases` — most conflicts are naming, resolvable by the existing alias mechanism.
4. Decide transport and identity (self-hosted endpoint vs. peer-to-peer over a user-chosen store). Revisit SPEC §2.2 non-goals explicitly before implementing.
5. Add a conflict inbox to the skill surface, reusing the existing inference-inbox and contradiction UX rather than inventing a second one.
6. Keep `worklog reprocess` as the recovery path: after any merge, interpretation can be rebuilt from evidence on each device.
