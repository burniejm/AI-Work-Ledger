import ast, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "work_ledger"

# DESIGN 3: four layers, imports may only point "up" the list.
LAYER0 = {"paths", "yamlite", "config", "ids", "timeutil", "textnorm", "redact",
          "credentials", "db", "db.connection", "db.migrations"}
UPPER  = {"entities", "work_items", "remaining", "decisions", "corrections",
          "contradictions", "hooks_runtime", "transcript", "gitevidence", "connections",
          "capture", "capture.events", "capture.checkpoints", "capture.sessions",
          "infer", "infer.stage_a", "infer.stage_b", "infer.confidence", "infer.dedupe",
          "infer.extract", "jira", "jira.adapter", "jira.rest", "jira.mcp",
          "jira.refresh", "jira.factory", "query", "query.daily", "query.status",
          "query.scoped", "query.render", "setupwizard", "backup", "doctor",
          "uninstall", "cli"}

def module_name(path):
    rel = path.relative_to(SRC).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)

def internal_imports(path):
    """Every work_ledger module this file imports AT MODULE SCOPE (function-local
    imports are deliberately excluded: they are the sanctioned cycle-break)."""
    tree = ast.parse(path.read_text())
    found = set()
    for node in tree.body:                              # module scope only
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("work_ledger."):
                    found.add(a.name[len("work_ledger."):])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:              # from .x import y / from ..x import y
                found.add(node.module)
            elif node.level and not node.module:        # from . import x
                found.update(a.name for a in node.names)
            elif node.module and node.module.startswith("work_ledger."):
                found.add(node.module[len("work_ledger."):])
    return found

class TestLayering(unittest.TestCase):
    def test_layer0_imports_nothing_from_the_upper_layers(self):
        for path in SRC.rglob("*.py"):
            name = module_name(path)
            if name not in LAYER0:
                continue
            for imported in internal_imports(path):
                head = imported.split(".")[0]
                self.assertNotIn(head, {u.split(".")[0] for u in UPPER},
                                 f"{name} (Layer 0) imports {imported}")

    def test_only_db_connection_opens_sqlite(self):
        # backup.py is a documented, reviewed exception (its own module
        # docstring, Ruling 9/16/17): `create_backup`/`restore` must move
        # data through `sqlite3.Connection.backup()`, which requires a
        # plain `sqlite3.connect()` handle to the backup/restore file on
        # the *other* side of the copy -- never the live, WAL-configured
        # application database, which is still only ever opened through
        # `db.connection.connect()`.
        allowed_extra = {"backup"}
        for path in SRC.rglob("*.py"):
            text = path.read_text()
            name = module_name(path)
            if name == "db.connection" or name in allowed_extra:
                continue
            self.assertNotIn("sqlite3.connect(", text,
                             f"{module_name(path)} opens its own SQLite connection")

    def test_only_redact_defines_credential_patterns(self):
        markers = ("ATATT", "ghp_", "xox", "AKIA", "PRIVATE KEY", "eyJ")
        for path in SRC.rglob("*.py"):
            if module_name(path) == "redact":
                continue
            text = path.read_text()
            for marker in markers:
                self.assertNotIn(marker, text,
                                 f"{module_name(path)} defines a credential pattern ({marker})")

    def test_only_jira_builds_http_requests(self):
        for path in SRC.rglob("*.py"):
            name = module_name(path)
            text = path.read_text()
            if name.startswith("jira."):
                continue
            for marker in ("urllib.request", "http.client", "urlopen("):
                self.assertNotIn(marker, text, f"{name} builds an HTTP request")

    def test_connections_does_not_import_a_jira_adapter(self):
        # Ruling 13: the connections <-> jira.mcp / jira.rest cycle is broken by
        # jira/factory.py. connections.py may reference jira.adapter (a leaf) only.
        imports = internal_imports(SRC / "connections.py")
        for forbidden in ("jira.rest", "jira.mcp", "jira.refresh", "jira.factory"):
            self.assertNotIn(forbidden, imports, f"connections.py imports {forbidden}")

    def test_the_internal_import_graph_is_acyclic(self):
        graph = {}
        for path in SRC.rglob("*.py"):
            graph[module_name(path)] = {i for i in internal_imports(path)}
        colour = {}
        stack = []

        def visit(node):
            colour[node] = "grey"; stack.append(node)
            for nxt in sorted(graph.get(node, ())):
                if nxt not in graph:
                    continue
                if colour.get(nxt) == "grey":
                    raise AssertionError("import cycle: " + " -> ".join(stack + [nxt]))
                if colour.get(nxt) is None:
                    visit(nxt)
            colour[node] = "black"; stack.pop()

        for node in sorted(graph):
            if colour.get(node) is None:
                visit(node)

    def test_textnorm_is_the_only_owner_of_the_stopword_list(self):
        # Ruling 23
        owners = [module_name(p) for p in SRC.rglob("*.py")
                  if "STOPWORDS" in p.read_text() and "textnorm" not in module_name(p)]
        self.assertEqual(owners, [], f"stopword list duplicated in {owners}")
