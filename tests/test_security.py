import ast, json, os, re, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "work_ledger"

SEEDED = ["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
          "ATATT3xFfGF0T4Jw9xQpLm2Zk8vRt6YbNc1Ud0Ha",
          "xoxb-123456789012-abcdefghijklmnop",
          "AKIAIOSFODNN7EXAMPLE", "hunter2hunter2", "s3cr3tpass"]

# --------------------------------------------------------------------------
# SQL-safety AST toolkit (Task 38 review round 1, items 2/3).
#
# The line-regex approach this test used to take could only ever exempt
# *whole files*, leaving every other interpolating line in an exempted file
# unproven, and it was blind to multi-line `execute(\n    f"...")` calls and
# to `execute(base + user_text)`-shaped concatenation. This replaces it with
# a full AST scan of every `execute`/`executescript`/`executemany` call in
# the src tree (which sees a call regardless of how its arguments are
# wrapped across lines), and a small, composable set of PROVEN-safe shapes:
#
#   - a plain string constant
#   - an f-string / string concatenation, provided every interpolated piece
#     is independently one of the shapes below
#   - an `int(...)` cast (can never contain SQL syntax, whatever feeds it)
#   - a `"sep".join(<genexp yielding only "?">)` placeholder list (only the
#     *count* of the source collection ever reaches the string)
#   - a `"sep".join(NAME)` where NAME is a local list/module constant built
#     only from the same safe shapes (a "sparse SET/WHERE clause")
#   - a bare Name resolving to a module-level constant built only from
#     string literals (optionally a pure filter over another such constant,
#     e.g. backup.py's `_CSV_TABLES = tuple(... for ... in EXPORT_TABLES ...)`)
#   - a bare Name that is a function PARAMETER, proved safe by recursively
#     checking every call site of that function in the same file
#   - a bare Name proved safe by an `if x not in TRUSTED_CONST: raise` guard
#     earlier in the same function (TRUSTED_MEMBERSHIP_CONSTANTS, each
#     itself proved literal -- see test_trusted_membership_constants_are_literal)
#   - a call to a same-file helper function, proved safe by recursively
#     checking every one of *its* `return` values
#
# Anything that cannot be shown safe by composing these rules is a real
# offender. A last-resort, per-LINE escape hatch (LINE_LEVEL_ESCAPE_HATCH)
# exists for the one shape that is not a source-text question at all: a
# migration's raw SQL, which lives in a `.sql` file on disk.

TRUSTED_MEMBERSHIP_CONSTANTS = {"SOFT_DELETABLE", "CORRECTABLE_FIELDS"}

LINE_LEVEL_ESCAPE_HATCH = {
    ("db/migrations.py", 293): (
        "stmt is one statement of a migration file's contents, read from disk "
        "by discover() at runtime -- never built from user, model, or Jira "
        "text, but its safety is a question of file provenance, not "
        "something visible in this repository's own source AST"),
    ("backup.py", 477): (
        "cols (via ','.join(cols)) is _table_columns(conn, table)'s live "
        "PRAGMA table_info() column list -- this project's own schema, "
        "reflected from the database at runtime, never caller/import-file "
        "input; table itself is separately proven safe (IMPORT_ORDER)"),
    ("backup.py", 487): (
        "sets is built from update_cols, itself filtered from the same "
        "PRAGMA table_info() column list as the entry above -- same "
        "database-schema provenance, not something visible in this "
        "repository's own source AST"),
}

def _module_level_assign(tree, name):
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
    return None

def _comprehension_is_pure_filter(node, tree):
    """True when a GeneratorExp/ListComp/SetComp's element is exactly its
    single generator's target(s), unchanged (a pure filter -- no data
    invented, nothing renamed), and its source iterable is itself proven
    literal-only. Covers backup.py's `_CSV_TABLES = tuple((table, pk) for
    table, pk in EXPORT_TABLES if table in _CSV_TABLE_NAMES)`."""
    if len(node.generators) != 1:
        return False
    gen = node.generators[0]

    def _names(n):
        if isinstance(n, ast.Name):
            return [n.id]
        if isinstance(n, ast.Tuple) and all(isinstance(e, ast.Name) for e in n.elts):
            return [e.id for e in n.elts]
        return None

    target_names, elt_names = _names(gen.target), _names(node.elt)
    if target_names is None or elt_names is None or target_names != elt_names:
        return False
    return _all_string_literals(gen.iter, tree)

def _all_string_literals(node, tree=None):
    """True when `node` is built exclusively from string literals, literal
    containers, a pure filter over another such source, or (when `tree` is
    given) a Name resolving to such a module-level constant."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return all(_all_string_literals(e, tree) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_all_string_literals(k, tree) and _all_string_literals(v, tree)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        return _comprehension_is_pure_filter(node, tree)
    if isinstance(node, ast.Call):                       # frozenset({...}) / set([...]) / tuple(...)
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in {"frozenset", "set", "tuple", "list", "dict"}:
            if node.args and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                return _comprehension_is_pure_filter(node.args[0], tree)
            return all(_all_string_literals(a, tree) for a in node.args)
        return False
    if isinstance(node, ast.Name) and tree is not None:
        src = _module_level_assign(tree, node.id)
        return src is not None and _all_string_literals(src, tree)
    return False

def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None

def _enclosing_function(tree, target):
    """The innermost `FunctionDef` in `tree` whose source range contains
    `target` (by line number), or None at module scope."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= target.lineno <= end and (best is None or node.lineno > best.lineno):
                best = node
    return best

def _resolve_to_tuple_or_list(node, tree):
    """Reduce `node` to the underlying `ast.Tuple`/`ast.List` it is built
    from -- resolving a bare Name (a module-level constant) and unwrapping
    a pure-filter comprehension (`tuple(x for x in SOURCE if ...)`, e.g.
    backup.py's `_CSV_TABLES`) over another such source -- or None when it
    is not reducible to one at all."""
    if isinstance(node, (ast.Tuple, ast.List)):
        return node
    if isinstance(node, ast.Name):
        return _resolve_to_tuple_or_list(_module_level_assign(tree, node.id), tree)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"tuple", "list"} and node.args
            and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp))):
        genexp = node.args[0]
        if len(genexp.generators) != 1:
            return None
        gen = genexp.generators[0]

        def _names(n):
            if isinstance(n, ast.Name):
                return [n.id]
            if isinstance(n, ast.Tuple) and all(isinstance(e, ast.Name) for e in n.elts):
                return [e.id for e in n.elts]
            return None

        if _names(gen.target) != _names(genexp.elt):
            return None       # not a pure passthrough filter -- give up
        return _resolve_to_tuple_or_list(gen.iter, tree)
    return None

def _resolve_literal_tuple_var(func_node, var_name, tree):
    """True when `var_name` is a `for`-loop target (alone, or one element of
    a tuple target) whose iterable reduces (see `_resolve_to_tuple_or_list`)
    to a tuple/list whose element AT VAR_NAME'S POSITION is always a string
    literal -- `var_name` can then only ever hold a string already literally
    present in this module's own source text. Position-aware, not
    "every element of every tuple is literal": `sessions.py`'s
    `for column, value in (("cwd", cwd), ...)` pairs a literal column name
    with a genuinely non-literal `value` at the OTHER position, and that
    must not make `column` unprovable."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.For):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == var_name:
            literal = _resolve_to_tuple_or_list(node.iter, tree)
            if literal is not None and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in literal.elts):
                return True
        elif isinstance(target, ast.Tuple):
            positions = [i for i, t in enumerate(target.elts)
                        if isinstance(t, ast.Name) and t.id == var_name]
            if not positions:
                continue
            pos = positions[0]
            literal = _resolve_to_tuple_or_list(node.iter, tree)
            if literal is not None and all(
                    isinstance(e, ast.Tuple) and pos < len(e.elts)
                    and isinstance(e.elts[pos], ast.Constant) and isinstance(e.elts[pos].value, str)
                    for e in literal.elts):
                return True
    return False

def _parent_function(tree, func_node):
    """The function lexically enclosing `func_node` (its closure's outer
    scope -- e.g. `backup.import_json`'s nested `def run():`), or None when
    `func_node` is already at module scope."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node is not func_node:
            end = getattr(node, "end_lineno", node.lineno)
            fend = getattr(func_node, "end_lineno", func_node.lineno)
            if node.lineno <= func_node.lineno and fend <= end:
                if best is None or node.lineno > best.lineno:
                    best = node
    return best

def _base_name_of(expr):
    """The trailing identifier of `expr` -- 'SOFT_DELETABLE' for a bare
    Name, an Attribute (`entities.SOFT_DELETABLE`), or a Subscript
    (`CORRECTABLE_FIELDS[table]`) -- else None."""
    if isinstance(expr, ast.Subscript):
        return _base_name_of(expr.value)
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return None

def _guarded_by_membership_check(func_node, var_name):
    """True when `func_node` contains `if var_name not in <trusted>: raise`
    -- directly, or as one clause of an `... or ...` test -- where
    `<trusted>` is one of TRUSTED_MEMBERSHIP_CONSTANTS. A raise means
    execution can only reach a later interpolation if the check held,
    regardless of which exact line that interpolation is on."""
    for node in ast.walk(func_node):
        if not (isinstance(node, ast.If) and any(isinstance(s, ast.Raise) for s in ast.walk(node))):
            continue
        tests = node.test.values if isinstance(node.test, ast.BoolOp) else [node.test]
        for test in tests:
            if (isinstance(test, ast.Compare) and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.NotIn)
                    and isinstance(test.left, ast.Name) and test.left.id == var_name
                    and _base_name_of(test.comparators[0]) in TRUSTED_MEMBERSHIP_CONSTANTS):
                return True
    return False

def _local_assignment_values(func_node, var_name):
    """Every RHS expression `var_name` is directly assigned in `func_node`:
    `var_name = X`, a chained `a = var_name = X`, or tuple-unpacking
    `var_name, ... = X, ...` (matched by position -- against either a
    literal tuple RHS, or a single call whose return this codebase's
    `(fragment, params, ...)`-returning helpers use, represented as a
    `("_CALL_ELEMENT_", call_node, index)` marker resolved by
    `_is_safe_sql_expr`). None means `var_name` is assigned in some other
    form this cannot statically resolve; [] means never assigned at all
    (e.g. a bare parameter)."""
    values = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == var_name:
                values.append(node.value)
            elif isinstance(target, ast.Tuple):
                for i, elt in enumerate(target.elts):
                    if isinstance(elt, ast.Name) and elt.id == var_name:
                        if isinstance(node.value, ast.Tuple) and i < len(node.value.elts):
                            values.append(node.value.elts[i])
                        elif isinstance(node.value, ast.Call):
                            values.append(("_CALL_ELEMENT_", node.value, i))
                        else:
                            return None
    return values

def _call_return_element_is_safe(call_node, tree, index, seen):
    """True when position `index` of every `return` value inside the
    same-file function `call_node` calls is provably safe (position 0 when
    a given `return` isn't a tuple at all) -- the counterpart to
    `_function_return_is_safe` for a caller that unpacks more than just the
    first element, e.g. `contradictions.py`'s
    `clause, params, short_circuit = _work_item_ids_clause(work_item_ids)`."""
    if not (isinstance(call_node, ast.Call) and isinstance(call_node.func, ast.Name)):
        return False
    name = call_node.func.id
    guard = ("return", name)
    if guard in seen:
        return False
    callee = _find_function(tree, name)
    if callee is None:
        return False
    seen = seen | {guard}
    found_any = False
    for node in ast.walk(callee):
        if isinstance(node, ast.Return) and node.value is not None:
            value = node.value
            if isinstance(value, ast.Tuple):
                if index >= len(value.elts):
                    return False
                candidate = value.elts[index]
            elif index == 0:
                candidate = value
            else:
                return False
            found_any = True
            if not _is_safe_sql_expr(candidate, callee, tree, seen):
                return False
    return found_any

def _is_placeholder_join(node):
    """`"sep".join(<genexp/listcomp yielding only the literal "?">)` -- safe
    regardless of what is being counted: only the *count* of the source
    collection ever reaches the string, never its contents."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str) and len(node.args) == 1):
        return False
    arg = node.args[0]
    return (isinstance(arg, (ast.GeneratorExp, ast.ListComp))
            and isinstance(arg.elt, ast.Constant) and arg.elt.value == "?")

def _is_function_parameter(func_node, var_name):
    return any(a.arg == var_name for a in func_node.args.args)

def _param_index(func_node, var_name):
    for i, a in enumerate(func_node.args.args):
        if a.arg == var_name:
            return i
    return None

def _param_default(func_node, index):
    """The default-value expression for the positional parameter at
    `index`, or None when it has none. `ast.arguments.defaults` is
    right-aligned to `args` (only the trailing N parameters can have
    defaults), so the mapping is by offset from the end."""
    args, defaults = func_node.args.args, func_node.args.defaults
    start = len(args) - len(defaults)
    return defaults[index - start] if index >= start else None

def _dict_values_are_literal(node, tree):
    """True when `node` builds a dict (a `{...}` literal, or `dict(SOURCE)`
    where SOURCE reduces to a tuple/list of 2-tuples) whose VALUES are all
    provably-literal SQL fragments -- regardless of its keys. Subscripting
    such a dict by *any* key can only ever return one of those literal
    values, or raise; the key itself is therefore irrelevant to safety."""
    if isinstance(node, ast.Dict):
        return all(_all_string_literals(v, tree) for v in node.values if v is not None)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"
            and len(node.args) == 1):
        source = _resolve_to_tuple_or_list(node.args[0], tree)
        if source is not None:
            return all(isinstance(e, ast.Tuple) and len(e.elts) == 2
                      and _all_string_literals(e.elts[1], tree) for e in source.elts)
    return False

def _is_safe_dict_container(name, func_node, tree, seen):
    """True when `name` resolves -- via the local/closure scope chain, or
    module scope -- to a dict-shaped expression whose values are all
    provably literal (`_dict_values_are_literal`). Used for a `Subscript`
    like `pk_by_table[table]` (backup.py's `import_json`): the dict itself
    is defined in the OUTER function and merely closed over by the nested
    `run()` that does the interpolating."""
    scope = func_node
    while scope is not None:
        assigns = _local_assignment_values(scope, name)
        if assigns is None:
            return False
        if assigns:
            return all(_dict_values_are_literal(a, tree) for a in assigns)
        scope = _parent_function(tree, scope)
    const = _module_level_assign(tree, name)
    return const is not None and _dict_values_are_literal(const, tree)

def _local_list_is_literal_only(func_node, var_name, tree, seen):
    """True when every element that can ever be in the local list variable
    `var_name` -- its initial literal (if any) plus every `.append()`/
    `.extend()` call against it -- inside `func_node` is itself a provably
    safe SQL fragment. Fails closed (False) when nothing is found at all."""
    exprs = []
    for node in ast.walk(func_node):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == var_name
                and isinstance(node.value, ast.List)):
            exprs.extend(node.value.elts)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == var_name
                and node.func.attr in ("append", "extend") and node.args):
            arg = node.args[0]
            if node.func.attr == "extend":
                if isinstance(arg, (ast.List, ast.Tuple)):
                    exprs.extend(arg.elts)
                    continue
                if isinstance(arg, ast.Name):
                    const = _module_level_assign(tree, arg.id)
                    if const is not None and _all_string_literals(const, tree):
                        continue
                    if _local_list_is_literal_only(func_node, arg.id, tree, seen):
                        continue
                return False
            exprs.append(arg)
    if not exprs:
        return False
    return all(_is_safe_sql_expr(e, func_node, tree, seen) for e in exprs)

def _all_call_sites_pass_safe_arg(tree, func_node, var_name, seen):
    """True when every call to `func_node`'s own function, anywhere else in
    this same file, passes a provably-safe expression for the parameter
    named `var_name` -- proves a function PARAMETER safe by tracing every
    place it is actually supplied (e.g. backup.py's
    `_table_columns(conn, table)`, called only with a `table` drawn from
    `EXPORT_TABLES`/`IMPORT_ORDER`), rather than assuming the callee's
    contract."""
    func_name = func_node.name
    index = _param_index(func_node, var_name)
    guard = ("param", func_name, var_name)
    if index is None or guard in seen:
        return False
    seen = seen | {guard}
    found_any = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) or not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == func_name):
            continue
        arg = node.args[index] if index < len(node.args) else next(
            (kw.value for kw in node.keywords if kw.arg == var_name), None)
        if arg is None:
            # The call site relies on the parameter's own default value
            # (e.g. contradictions.py's `_work_item_ids_clause(work_item_ids)`
            # never overrides `column="id"`) -- the default itself must be
            # proven safe, evaluated at module scope since a default
            # expression cannot reference the function's own locals.
            default = _param_default(func_node, index)
            if default is None or not _is_safe_sql_expr(default, None, tree, seen):
                return False
            found_any = True
            continue
        found_any = True
        if not _is_safe_sql_expr(arg, _enclosing_function(tree, node), tree, seen):
            return False
    return found_any

def _function_return_is_safe(func_node, tree, seen):
    """True when every `return` in `func_node` whose value could be the SQL
    fragment a caller embeds directly (the value itself, or the first
    element of a returned tuple -- the `(fragment, params)` shape every
    clause-building helper in this codebase uses) is itself provably safe."""
    found_any = False
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            value = node.value
            candidate = value.elts[0] if isinstance(value, ast.Tuple) and value.elts else value
            found_any = True
            if not _is_safe_sql_expr(candidate, func_node, tree, seen):
                return False
    return found_any

def _is_safe_sql_expr(node, func_node, tree, seen=None):
    """The main recursive prover: True when `node` (an expression appearing
    inside, or forming the whole of, an execute()-family SQL-string
    argument) can only ever evaluate to a fixed fragment/value never built
    from user, model, or Jira text. `func_node` is the enclosing function
    (None at module scope); `tree` is that file's whole module AST."""
    if seen is None:
        seen = set()

    if isinstance(node, tuple) and node and node[0] == "_CALL_ELEMENT_":
        _, call_node, index = node
        return _call_return_element_is_safe(call_node, tree, index, seen)

    # Fast path: anything _all_string_literals already proves outright (a
    # literal, a literal container, or a pure filter over one).
    if _all_string_literals(node, tree):
        return True

    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, bool)) or node.value is None

    if isinstance(node, ast.JoinedStr):
        return all(_is_safe_sql_expr(p.value, func_node, tree, seen)
                   for p in node.values if isinstance(p, ast.FormattedValue))

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (_is_safe_sql_expr(node.left, func_node, tree, seen)
                and _is_safe_sql_expr(node.right, func_node, tree, seen))

    if isinstance(node, ast.IfExp):
        # A ternary is safe regardless of its condition when BOTH branches
        # are (backup.py's `dedupe_col = "idempotency_key" if table ==
        # "source_events" else None`).
        return (_is_safe_sql_expr(node.body, func_node, tree, seen)
                and _is_safe_sql_expr(node.orelse, func_node, tree, seen))

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "int" and node.args:
            return True
        if _is_placeholder_join(node):
            return True
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.args):
            # dict.get(key) -- like a Subscript, can only ever return one of
            # the dict's own literal values, or the get() default/None
            # (backup.py's `_SELF_REFERENCE_COLUMNS.get(table)`).
            default_ok = len(node.args) < 2 or _is_safe_sql_expr(node.args[1], func_node, tree, seen)
            return default_ok and _is_safe_dict_container(node.func.value.id, func_node, tree, seen)
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "join"
                and isinstance(node.func.value, ast.Constant)
                and isinstance(node.func.value.value, str) and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)):
            name = node.args[0].id
            if func_node is not None and _local_list_is_literal_only(func_node, name, tree, seen):
                return True
            const = _module_level_assign(tree, name)
            return const is not None and _all_string_literals(const, tree)
        if isinstance(node.func, ast.Name) and ("return", node.func.id) not in seen:
            callee = _find_function(tree, node.func.id)
            if callee is not None:
                return _function_return_is_safe(callee, tree, seen | {("return", node.func.id)})
        return False

    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        # Subscripting a dict whose *values* are all literal can only ever
        # return one of those literal values (or raise) -- whatever
        # supplies the key is irrelevant to that guarantee (backup.py's
        # `pk_by_table[table]`, where `pk_by_table = dict(EXPORT_TABLES)`).
        return _is_safe_dict_container(node.value.id, func_node, tree, seen)

    if isinstance(node, ast.Name):
        # Walk the lexical scope chain: `func_node` first, then each
        # enclosing (closure) function, then module scope. A nested helper
        # like backup.py's `import_json.run()` routinely reads a name (e.g.
        # `pk_by_table`) assigned in its OUTER function, not its own body.
        scope = func_node
        while scope is not None:
            if _guarded_by_membership_check(scope, node.id):
                return True
            if _resolve_literal_tuple_var(scope, node.id, tree):
                return True
            assigns = _local_assignment_values(scope, node.id)
            if assigns is None:
                return False
            if assigns:
                return all(_is_safe_sql_expr(a, scope, tree, seen) for a in assigns)
            if _is_function_parameter(scope, node.id):
                return _all_call_sites_pass_safe_arg(tree, scope, node.id, seen)
            scope = _parent_function(tree, scope)
        const = _module_level_assign(tree, node.id)
        return const is not None and _all_string_literals(const, tree)

    return False

def _sql_execute_calls(tree):
    """Every `execute`/`executescript`/`executemany` call in `tree`, paired
    with its first (SQL-string) argument. AST-based, so this sees a call
    regardless of how many lines its arguments are spread across -- the
    line-regex this replaced could not."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executescript", "executemany")
                and node.args):
            yield node, node.args[0]

class TestSecurity(unittest.TestCase):
    def test_seeded_secrets_never_reach_db_logs_exports_or_backups(self):
        # Drive a full fake session through the hook runtime with each secret in tool
        # output, run Stage A, back up, export json/markdown/csv, then grep EVERY
        # produced artifact (raw database bytes included) for each literal.
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "wl"
            os.environ["WORK_LEDGER_HOME"] = str(home)
            try:
                sys.path.insert(0, str(ROOT / "src"))
                from work_ledger import hooks_runtime, backup, paths, redact, credentials
                from work_ledger.db import connection as dbc, migrations as m
                from work_ledger.capture import sessions as S
                from work_ledger.infer import stage_a

                paths.ensure_dirs()
                conn = dbc.connect(str(paths.db_path())); m.apply_all(conn)

                # The last two SEEDED literals (see the module comment above)
                # are bare, human-chosen-looking passwords with no vendor
                # shape and no "name=value" context anywhere in the payloads
                # below -- nothing a *pattern* can safely recognize (Task 38
                # review: a shape-based pattern broad enough to catch these
                # was proven to also corrupt ordinary branch names, image
                # tags, and commit subjects). Standing in for the realistic
                # way such a secret becomes "known" to this process --
                # someone configured it as an actual credential at some
                # point -- resolve each one through `credentials.resolve()`
                # via an `env://` reference before the leak ever happens;
                # `resolve()` registers every value it returns with
                # `redact.register_known_secret`, which is what lets
                # `redact()` catch it verbatim regardless of shape.
                redact.clear_known_secrets()
                os.environ["WL_TEST_CRED_A"] = SEEDED[4]
                os.environ["WL_TEST_CRED_B"] = SEEDED[5]
                self.assertEqual(credentials.resolve("env://WL_TEST_CRED_A"), SEEDED[4])
                self.assertEqual(credentials.resolve("env://WL_TEST_CRED_B"), SEEDED[5])

                hooks_runtime.handle("session-start", {
                    "session_id": "leak-1", "cwd": str(ROOT), "transcript_path": str(Path(d) / "t.jsonl")})
                for i, secret in enumerate(SEEDED):
                    hooks_runtime.handle("post-tool-use", {
                        "session_id": "leak-1", "cwd": str(ROOT), "tool_name": "Bash",
                        "tool_use_id": f"toolu_{i}",
                        "tool_input": {"command": f"npm test  # {secret}"},
                        "tool_response": {"stdout": f"leaked {secret}\n", "stderr": secret,
                                          "exit_code": 1}})
                # a secret arriving through a manual statement must be redacted too
                from work_ledger import manual
                manual.record(conn, manual.parse_add(f"Reviewed AB-76 using {SEEDED[0]}"))
                hooks_runtime.handle("stop", {"session_id": "leak-1", "cwd": str(ROOT)})
                stage_a.run(conn, "leak-1")

                # a secret must not survive a deliberate raise through the hook logger
                hooks_runtime.log_exception(RuntimeError(f"boom {SEEDED[1]}"), "test-context")

                bpath = backup.create_backup(conn, label="leaktest")
                out_json = paths.export_dir() / "export.json"
                backup.export_json(conn, out_json)
                out_md = paths.export_dir() / "history.md"
                backup.export_markdown(conn, out_md, tz="America/Chicago", rollover_hour=3)
                csv_files = backup.export_csv(conn, paths.export_dir() / "csv")
                conn.close()

                artifacts = [paths.db_path(), bpath, out_json, out_md, *csv_files]
                artifacts += list(paths.log_dir().rglob("*"))
                artifacts += [p for p in home.rglob("*") if p.is_file()]

                checked = 0
                for p in artifacts:
                    p = Path(p)
                    if not p.is_file():
                        continue
                    blob = p.read_bytes()
                    checked += 1
                    for secret in SEEDED:
                        self.assertNotIn(secret.encode(), blob,
                                         f"{secret!r} leaked into {p}")
                self.assertGreaterEqual(checked, 5, "the sweep must actually inspect artifacts")

                # and the redaction actually fired rather than the payloads being dropped
                c2 = sqlite3.connect(str(paths.db_path()))
                joined = " ".join(r[0] or "" for r in
                                  c2.execute("SELECT redaction_json FROM source_events"))
                self.assertIn("github_token", joined)
                self.assertIn("known_secret", joined,
                             "the known-secret registry must be what caught the bare passwords")
            finally:
                del os.environ["WORK_LEDGER_HOME"]
                del os.environ["WL_TEST_CRED_A"]
                del os.environ["WL_TEST_CRED_B"]
                redact.clear_known_secrets()

    def test_ordinary_content_survives_redaction(self):
        # Task 38 review: the leak sweep above only greps for seeded secret
        # literals -- it is blind to over-redaction. A prior version of
        # `redact.py` shipped a shape-based "generic secret" pattern that
        # corrupted exactly this kind of ordinary content (branch names,
        # commit subjects, paths, image/pod names, and even credential
        # *references*, which must never be touched at all). These are the
        # reviewer-verified regressions from that pattern, plus a few more
        # of the same shape; every one must come back byte-for-byte
        # unchanged, with nothing reported as having fired.
        sys.path.insert(0, str(ROOT / "src"))
        from work_ledger import redact

        ordinary = [
            "release2026branch",
            "feature/release2026branch",
            "acme2026",
            "libpython3",
            "/tmp/scratch2026abc",
            "ubuntu2204",
            "coredns7db6d8ff4d",
            "keychain://work-ledger/jira-acme2026",
            "env://JIRA_CONNECTION_NAME2026",
            "AB-76",
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # sha256-shaped hex
            "018f3a2b-7c1e-7000-8f21-abcdef123456",                              # uuidv7-shaped
            "toolu_01ABC2026xyz",
            "fixed upload retry for ab-76 in release2026branch",
        ]
        for text in ordinary:
            with self.subTest(text=text):
                redacted, fired = redact.redact(text)
                self.assertEqual(redacted, text, f"{text!r} was altered: {redacted!r}")
                self.assertEqual(fired, [], f"{text!r} fired pattern(s) {fired}")

    def test_a_reference_prefix_does_not_bypass_scanning_a_longer_string(self):
        # Fix round 2: redact()/looks_like_secret() used to skip scanning
        # the WHOLE string whenever it merely STARTED WITH a keychain://
        # or env:// reference, so a real secret concatenated after one
        # sailed through untouched -- verified bypass:
        # redact("env://JIRA_TOKEN resolved to ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        # returned the GitHub token verbatim. Only a BARE reference (the
        # whole string, nothing else) is exempt; a reference embedded
        # alongside other text must be scanned normally.
        sys.path.insert(0, str(ROOT / "src"))
        from work_ledger import redact

        leaked = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        mixed_cases = [
            f"env://JIRA_TOKEN resolved to {leaked}",
            f"keychain://work-ledger/jira resolved to {leaked}",
            f"using {leaked} via env://JIRA_TOKEN",
        ]
        for text in mixed_cases:
            with self.subTest(text=text):
                redacted, fired = redact.redact(text)
                self.assertNotIn(leaked, redacted, f"secret survived in {redacted!r}")
                self.assertIn("github_token", fired)
                self.assertFalse(redact.looks_like_secret(
                    "env://JIRA_TOKEN"), "a bare reference alone must never look like a secret")
                self.assertTrue(redact.looks_like_secret(text),
                                f"a reference-plus-secret string must still look like a secret: {text!r}")

        # a BARE reference -- the whole string, nothing else -- must still
        # survive completely untouched (this is the case the guard exists for).
        for bare in ("env://JIRA_TOKEN", "keychain://work-ledger/jira-acme2026"):
            redacted, fired = redact.redact(bare)
            self.assertEqual(redacted, bare)
            self.assertEqual(fired, [])

    def test_no_sql_is_built_from_input(self):
        # AST-based (see the toolkit above): sees every execute()-family call
        # regardless of how many lines its arguments span, and proves each
        # non-constant argument safe by construction rather than skipping
        # whole files. Anything not provably safe, and not on the tiny,
        # justified LINE_LEVEL_ESCAPE_HATCH, is a real offender.
        offenders = []
        for p in sorted(SRC.rglob("*.py")):
            tree = ast.parse(p.read_text())
            rel = str(p.relative_to(SRC))
            for call, arg in _sql_execute_calls(tree):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    continue
                if (rel, call.lineno) in LINE_LEVEL_ESCAPE_HATCH:
                    continue
                func_node = _enclosing_function(tree, call)
                if _is_safe_sql_expr(arg, func_node, tree):
                    continue
                offenders.append(f"{rel}:{call.lineno}: {ast.unparse(arg)}")
        self.assertEqual(offenders, [], "possible SQL string interpolation:\n" + "\n".join(offenders))

    def test_escape_hatch_entries_are_still_accurate(self):
        # LINE_LEVEL_ESCAPE_HATCH must stay honest: every entry has to still
        # name a real execute()-family call with a non-constant argument, or
        # it is stale (the line moved, or became a plain literal) and must
        # be removed rather than silently trusted forever.
        remaining = dict(LINE_LEVEL_ESCAPE_HATCH)
        for p in sorted(SRC.rglob("*.py")):
            tree = ast.parse(p.read_text())
            rel = str(p.relative_to(SRC))
            for call, arg in _sql_execute_calls(tree):
                key = (rel, call.lineno)
                if key in remaining and not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    del remaining[key]
        self.assertEqual(remaining, {}, f"stale escape-hatch entries: {remaining}")

    def test_trusted_membership_constants_are_literal(self):
        # `_guarded_by_membership_check` trusts exactly these two constants
        # as "validated before use" guards (SOFT_DELETABLE in entities.py,
        # CORRECTABLE_FIELDS in corrections.py) -- prove each is built from
        # string literals only, never assume it.
        sources = {"entities.py": ("SOFT_DELETABLE",), "corrections.py": ("CORRECTABLE_FIELDS",)}
        self.assertEqual({n for names in sources.values() for n in names}, TRUSTED_MEMBERSHIP_CONSTANTS)
        for filename, names in sources.items():
            path = next(SRC.rglob(filename))
            tree = ast.parse(path.read_text())
            for name in names:
                node = _module_level_assign(tree, name)
                self.assertIsNotNone(node, f"{filename}: {name} is not a module-level assignment")
                self.assertTrue(_all_string_literals(node, tree),
                                f"{filename}: {name} is not built from string literals only")

    def test_no_jira_write_verbs_anywhere(self):
        for p in (SRC / "jira").rglob("*.py"):
            text = p.read_text()
            for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"',
                         "createJiraIssue", "transitionJiraIssue", "addCommentToJiraIssue",
                         "addWorklogToJiraIssue", "editJiraIssue", "createIssueLink"):
                self.assertNotIn(verb, text, f"{p} references {verb}")

    def test_no_mutating_git_commands(self):
        pattern = re.compile(r'\[["\']git["\'][^\]]*["\'](commit|push|merge|rebase|reset|checkout|add)["\']')
        for p in SRC.rglob("*.py"):
            self.assertIsNone(pattern.search(p.read_text()), f"{p} may mutate a repository")

    def test_no_shell_true(self):
        for p in SRC.rglob("*.py"):
            self.assertNotIn("shell=True", p.read_text(), f"{p} uses shell=True")

    def test_hook_entry_point_never_exits_nonzero(self):
        # Ruling 26: there are no hooks/*.py shims any more. The single hook entry
        # point is `worklog hook <event>`, so assert on its behaviour, not on shim text.
        self.assertEqual(sorted(p.name for p in (ROOT / "hooks").iterdir()), ["hooks.json"])
        cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        subs = {h["command"].rsplit(" ", 1)[1]
                for groups in cfg["hooks"].values() for g in groups for h in g["hooks"]}
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "WORK_LEDGER_HOME": d, "CLAUDE_PLUGIN_ROOT": str(ROOT)}
            env.pop("WORK_LEDGER_NESTED", None)
            for sub in sorted(subs) + ["not-a-real-event"]:
                for stdin in ("", "not json", "[]", '{"session_id": null}'):
                    p = subprocess.run([sys.executable, str(ROOT / "bin" / "worklog"), "hook", sub],
                                       input=stdin, capture_output=True, text=True, timeout=30, env=env)
                    self.assertEqual(p.returncode, 0,
                                     f"hook {sub} with stdin {stdin!r} exited {p.returncode}")
                    self.assertEqual(p.stdout, "", f"hook {sub} wrote to stdout")
        # and the source of the handler never raises SystemExit with a non-zero code
        cli_src = (SRC / "cli.py").read_text()
        for forbidden in ("SystemExit(2)", "sys.exit(1)", "sys.exit(2)"):
            self.assertNotIn(forbidden, cli_src)

    def test_data_files_are_0600_and_dirs_0700(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "wl"
            os.environ["WORK_LEDGER_HOME"] = str(home)
            try:
                sys.path.insert(0, str(ROOT / "src"))
                from work_ledger import paths, backup, config
                from work_ledger.db import connection as dbc, migrations as m
                paths.ensure_dirs()
                config.load()                                    # writes config.yaml
                conn = dbc.connect(str(paths.db_path())); m.apply_all(conn)
                backup.create_backup(conn, label="perms")
                out = paths.export_dir() / "export.json"
                backup.export_json(conn, out)
                conn.close()

                dirs = [home, paths.backup_dir(), paths.log_dir(),
                        paths.state_dir(), paths.export_dir()]
                for p in dirs:
                    self.assertTrue(p.is_dir(), p)
                    self.assertEqual(oct(p.stat().st_mode & 0o777), "0o700", f"{p} is not 0700")

                files = [paths.config_path(), paths.db_path(), out]
                files += list(paths.backup_dir().glob("*.sqlite"))
                self.assertGreaterEqual(len(files), 4)
                for p in files:
                    self.assertTrue(p.is_file(), p)
                    self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600", f"{p} is not 0600")
            finally:
                del os.environ["WORK_LEDGER_HOME"]
