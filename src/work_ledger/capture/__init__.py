"""Hook-facing capture layer: source_events ingestion and capture_checkpoints.

Everything a hook writes to SQLite goes through this package (Global
Constraints: "hooks may only append source_events, upsert sessions ...,
write capture_checkpoints"). `events.append_source_event` is the required
redaction choke point (Ruling 15) for anything captured from a hook.
"""
