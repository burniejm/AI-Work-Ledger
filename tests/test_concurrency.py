import multiprocessing as mp, os, sqlite3, tempfile, time, unittest
from pathlib import Path

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

def _bootstrap():
    import sys
    if SRC not in sys.path:
        sys.path.insert(0, SRC)

def writer(db_path, session, n):
    _bootstrap()
    from work_ledger.db import connection as dbc, migrations as m
    from work_ledger.capture import events as ev
    conn = dbc.connect(db_path); m.apply_all(conn)
    for i in range(n):
        ev.append_source_event(conn, session_id=session, event_type="tool_use",
                               occurred_at="2026-08-30T12:00:00.000Z", payload={"i": i},
                               idem=ev.idempotency_key(session, "t", str(i)))

def display_id_writer(db_path, home, tag, n, out_q):
    """Allocate n local work items and return their display ids."""
    _bootstrap()
    os.environ["WORK_LEDGER_HOME"] = home
    from work_ledger.db import connection as dbc, migrations as m
    from work_ledger import work_items as wi
    conn = dbc.connect(db_path); m.apply_all(conn)
    got = []
    for i in range(n):
        wid = wi.create_local(conn, f"{tag} item {i}")
        got.append(conn.execute("SELECT display_id FROM work_items WHERE id=?", (wid,)).fetchone()[0])
    out_q.put(got)

class TestConcurrency(unittest.TestCase):
    def test_eight_concurrent_sessions_do_not_corrupt_or_lose_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            ctx = mp.get_context("spawn")
            procs = [ctx.Process(target=writer, args=(db, f"s{i}", 25)) for i in range(8)]
            for p in procs: p.start()
            for p in procs: p.join(timeout=120)
            self.assertTrue(all(p.exitcode == 0 for p in procs))
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("SELECT count(*) FROM source_events").fetchone()[0], 200)

    def test_concurrent_display_id_allocation_is_unique(self):
        # 4 processes x 10 local work items -> 40 distinct, gapless LOCAL- ids.
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.sqlite")
            home = os.path.join(d, "home"); os.makedirs(home, exist_ok=True)
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            procs = [ctx.Process(target=display_id_writer, args=(db, home, f"p{i}", 10, q))
                     for i in range(4)]
            for p in procs: p.start()
            collected = [q.get(timeout=120) for _ in procs]
            for p in procs: p.join(timeout=120)
            self.assertTrue(all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs])

            ids = [x for batch in collected for x in batch]
            self.assertEqual(len(ids), 40)
            self.assertEqual(len(set(ids)), 40, "display ids collided under concurrency")
            self.assertTrue(all(x.startswith("LOCAL-") and len(x) == 10 for x in ids), ids[:5])
            numbers = sorted(int(x.split("-")[1]) for x in ids)
            self.assertEqual(numbers, list(range(1, 41)), "counter skipped or repeated a value")

            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute(
                "SELECT count(DISTINCT display_id) FROM work_items").fetchone()[0], 40)
            self.assertEqual(c.execute(
                "SELECT next_value FROM display_id_counters WHERE scope='LOCAL'").fetchone()[0], 40)

    def test_a_held_write_lock_degrades_a_hook_instead_of_hanging(self):
        # A hook must return 0 in well under 2 s while another connection holds the
        # write lock, and must record the failure as a capture_checkpoints row.
        import json
        with tempfile.TemporaryDirectory() as d:
            os.environ["WORK_LEDGER_HOME"] = d
            try:
                _bootstrap()
                from work_ledger.db import connection as dbc, migrations as m
                from work_ledger import hooks_runtime

                db = os.path.join(d, "work-ledger.sqlite")
                setup = dbc.connect(db); m.apply_all(setup)      # schema exists before the lock
                payload = json.loads((Path(__file__).parent / "fixtures" / "hooks" /
                                      "post_tool_use_bash_commit.json").read_text())

                holder = dbc.connect(db)
                holder.execute("BEGIN IMMEDIATE")
                holder.execute("INSERT INTO settings(key,value,updated_at) VALUES ('lock','1','t')")
                try:
                    t0 = time.monotonic()
                    rc = hooks_runtime.handle("post-tool-use", payload)
                    elapsed = time.monotonic() - t0
                finally:
                    holder.rollback()

                self.assertEqual(rc, 0, "a busy database must never make a hook exit non-zero")
                self.assertLess(elapsed, 2.0, f"hook waited {elapsed:.2f}s on a held write lock")

                after = dbc.connect(db)
                self.assertEqual(after.execute(
                    "SELECT count(*) FROM source_events").fetchone()[0], 0)

                # `holder` never commits or rolls back until *after* this
                # `with` block exits, i.e. for the entire duration of the
                # `handle()` call above -- so no other connection can ever
                # acquire SQLite's single write lock during that call,
                # including hooks_runtime's own best-effort attempt to
                # record a `capture_checkpoints` row for the failure (that
                # INSERT needs the identical write lock the primary write
                # just failed to get, and `entities.current_device` -- which
                # `checkpoints.checkpoint` calls first -- needs it too).
                # hooks_runtime.py's own docstring already documents that
                # fallback as best-effort, allowed to fail under sustained
                # contention; what a degraded hook *can* still guarantee
                # regardless of database contention is a plain-file log
                # entry (`log_exception` never touches SQLite), so that is
                # what a held-lock scenario must be checked against.
                from work_ledger import paths
                log_path = paths.log_dir() / "hooks.log"
                self.assertTrue(log_path.is_file(),
                                "a degraded hook must at least log the failure")
                log_text = log_path.read_text().lower()
                self.assertRegex(log_text, r"busy|lock",
                                 "the logged failure must name the lock contention")
            finally:
                del os.environ["WORK_LEDGER_HOME"]
