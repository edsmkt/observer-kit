#!/usr/bin/env python3
"""Lint an agent-written batch script for two observer-kit violations:

    buffering all provider results in memory and emitting `record` ledger
    rows only in a final flush block (instead of as work lands).

    reporting progress while the actual result remains memory-only until a
    final write. A live dashboard is not a durable resume point.

This defeats live dashboard visibility and loses everything if the process
crashes mid-run. Run it on any script before the full run:

    python3 references/lint_emit.py path/to/script.py
Exit code 0 = OK, 1 = violation found (CI should fail the full run).

Heuristic (intentionally simple, stdlib-only):
  A script is SUSPECT if it calls ledger(... 'record' ...) but NONE of those
  calls are statically inside a per-item loop (for/while whose body or a called
  function emits record events). We treat a record-emit as "inside the loop" if
  the emit call's enclosing function is invoked from a loop, OR the emit call
  is lexically inside a for/while that ranges over the work items.

Because agents write many shapes, we also look for the canonical smell:
  - a results dict/list is populated inside a loop, AND
  - the only ledger('record') calls are in a later block that ranges over the
    same items (a flush), with no emit inside the loop.
  - a results container is populated in a work loop but there is no apparent
    durable sink call in that loop or completion callback. Progress/metric
    ledger calls alone do not count as persistence.
"""
import argparse
import ast
import sys

RECORD_EVENTS = {'record'}


def _is_ledger_record_call(node):
    """Return True if `node` is a call that emits a 'record' ledger event."""
    if not isinstance(node, ast.Call):
        return False
    # ledger(scope, 'record', ...)
    if isinstance(node.func, ast.Name) and node.func.id == 'ledger':
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value in RECORD_EVENTS
    # run.step(..., event='record') or run.step('record', ...)
    if isinstance(node.func, ast.Attribute) and node.func.attr in ('step', 'record'):
        # check positional or keyword for 'record'
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in RECORD_EVENTS:
            return True
        for kw in node.keywords:
            if kw.arg in ('event', 'table') and isinstance(kw.value, ast.Constant) and kw.value.value in RECORD_EVENTS:
                return True
    return False


def _loop_ranges_over_work(node, work_names):
    """Heuristic: is this for/while looping over something that looks like the
    work set (todo / items / companies / results.values() / futures)?"""
    it = None
    if isinstance(node, ast.For):
        it = node.iter
    elif isinstance(node, ast.While):
        return False  # while loops don't range over a known collection
    if it is None:
        return False
    src = ast.dump(it)
    for w in work_names:
        if w in src:
            return True
    # common iterables: results_by_vat.values(), todo, items, companies
    if any(x in src for x in ('.values()', 'as_completed', 'futures', 'results')):
        return True
    return False


def analyze(path):
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)

    work_names = {'todo', 'items', 'companies', 'contacts', 'rows', 'batch',
                  'results', 'futures', 'todo_list', 'work'}

    record_emit_sites = []  # (func_name, node)
    for node in ast.walk(tree):
        if _is_ledger_record_call(node):
            fn = _enclosing_function(tree, node)
            fname = fn.name if fn else '<module>'
            record_emit_sites.append((fname, node))

    # Find the loop(s) that mutate a results container (the "work" loop)
    work_loops = _find_result_mutating_loops(tree, work_names)

    # A result held only in memory after a provider phase is neither resumable
    # nor durable, even if the script emits lively progress heartbeats. Require
    # an apparent sink call from that loop (or a helper it invokes).
    durability_violations = [
        ('DURABILITY_MISSING', loop.lineno)
        for loop in work_loops
        if not _loop_has_durable_write(loop, tree)
    ]

    if not record_emit_sites:
        return durability_violations

    # A record emit is VALID only if it is inside a work loop (the same loop that
    # mutates results), or inside a function called from a work loop.
    violations = []
    for fname, node in record_emit_sites:
        if not work_loops:
            # No buffering detected — emit-in-any-work-loop is the correct pattern.
            if _emit_in_any_work_loop(node, tree, work_names):
                continue
        else:
            if _emit_in_work_loop(node, tree, work_loops, work_names):
                continue
        violations.append((fname, node.lineno))

    # Buffered-flush smell: a results container is mutated in a work loop but
    # record emits are NOT inside that loop (separate flush loop / outside).
    buffered = _detect_buffered_flush(tree, work_names)
    if buffered and violations:
        violations.append(('BUFFERED_FLUSH', buffered))

    return violations + durability_violations


def _enclosing_function(tree, node):
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if _node_in_func(fn, node):
            return fn
    return None


def _node_in_func(func, node):
    for n in ast.walk(func):
        if n is node:
            return True
    return False


def _find_result_mutating_loops(tree, work_names):
    """Return the set of for/while loops that (a) range over work items and
    (b) mutate a results container via .append/.extend/.update/.add OR a
    subscript assignment like results[key] = value."""
    found = []
    for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]:
        if not _loop_ranges_over_work(loop, work_names):
            continue
        for n in ast.walk(loop):
            # .append / .extend / .update / .add on a results container
            if isinstance(n, ast.Attribute) and n.attr in ('append', 'extend', 'update', 'add'):
                if 'result' in ast.dump(n.value):
                    found.append(loop)
                    break
            # results[key] = value  (subscript assignment)
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and 'result' in ast.dump(t.value):
                        found.append(loop)
                        break
        if found and found[-1] is loop:
            break
    return found


def _emit_in_work_loop(node, tree, work_loops, work_names):
    """Emit is valid if it is lexically inside a work loop, or inside a function
    called from a work loop."""
    for loop in work_loops:
        if _node_in_loop_body(loop, node):
            return True
    fn = _enclosing_function(tree, node)
    if fn:
        for loop in work_loops:
            if _func_called_from_loop_body(loop, fn.name):
                return True
    return False


def _emit_in_any_work_loop(node, tree, work_names):
    """Emit is valid if it is lexically inside any loop that ranges over work
    items (no buffering detected, so live emit from the work loop is correct)."""
    for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]:
        if _node_in_loop_body(loop, node) and _loop_ranges_over_work(loop, work_names):
            return True
    fn = _enclosing_function(tree, node)
    if fn:
        for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]:
            if _loop_ranges_over_work(loop, work_names) and _func_called_from_loop_body(loop, fn.name):
                return True
    return False


def _loop_has_durable_write(loop, tree):
    """Return whether a result-mutating loop appears to persist its result.

    This is intentionally a conservative static heuristic. A helper such as
    append_result(), save_row(), write_to_sheet(), or a direct file/database
    write is enough to pass; progress(), count(), checkpoint(), and ledger()
    are observability only and deliberately do not count.
    """
    for call in [n for n in ast.walk(loop) if isinstance(n, ast.Call)]:
        if _is_durable_write_call(call):
            return True
        called = _called_name(call)
        if called and _helper_has_durable_write(tree, called):
            return True
    return False


def _called_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_durable_write_call(call):
    name = (_called_name(call) or '').lower()
    if name in {'ledger', 'progress', 'count', 'checkpoint', 'metric', 'step'}:
        return False
    # Generic but intentionally broad names used by local files, DBs, sheets,
    # APIs, and explicit durable checkpoint helpers.
    durable_words = ('write', 'append', 'insert', 'upsert', 'persist', 'save',
                     'commit', 'receipt', 'checkpoint', 'dump', 'store')
    if any(word in name for word in durable_words):
        return True
    return False


def _helper_has_durable_write(tree, name):
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if fn.name != name:
            continue
        return any(_is_durable_write_call(call)
                   for call in ast.walk(fn) if isinstance(call, ast.Call))
    return False


def _node_in_loop_body(loop, node):
    for child in ast.iter_child_nodes(loop):
        if child is loop.iter:
            continue
        for n in ast.walk(child):
            if n is node:
                return True
    return False


def _func_called_from_loop_body(loop, fname):
    for call in [n for n in ast.walk(loop) if isinstance(n, ast.Call)]:
        called = None
        if isinstance(call.func, ast.Name):
            called = call.func.id
        elif isinstance(call.func, ast.Attribute):
            called = call.func.attr
        if called == fname:
            return True
    return False


def _detect_buffered_flush(tree, work_names):
    """Detect: a results container mutated inside a work loop, but record emits
    only happen in a separate loop or outside any loop."""
    for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]:
        if not _loop_ranges_over_work(loop, work_names):
            continue
        for n in ast.walk(loop):
            if isinstance(n, ast.Attribute) and n.attr in ('append', 'extend', 'update', 'add'):
                base = ast.dump(n.value)
                if 'result' in base:
                    return base
    return None


def main():
    ap = argparse.ArgumentParser(description='Lint for buffered-then-flush ledger violation')
    ap.add_argument('script')
    args = ap.parse_args()

    try:
        violations = analyze(args.script)
    except SyntaxError as e:
        print(f'SYNTAX ERROR in {args.script}: {e}')
        sys.exit(2)

    if not violations:
        print(f'OK — {args.script}: record events and durable results are emitted inside work loops.')
        sys.exit(0)

    print(f'VIOLATION in {args.script}: incremental observability or durability is missing.')
    if any(kind == 'DURABILITY_MISSING' for kind, _ in violations):
        print('  DURABILITY MISSING: a results container is populated in a work loop,')
        print('  but no durable result write is visible there. Progress events do not')
        print('  protect paid work from a crash or make --resume skip it.')
    if any(kind != 'DURABILITY_MISSING' for kind, _ in violations):
        print('  RECORD EMIT MISSING: record ledger events are outside the work loop.')
    print()
    print('  Fix: persist the result and emit its record in the same item loop or')
    print('  completion callback, then checkpoint only after that durable boundary.')
    print()
    for fname, lineno in violations:
        if fname != 'DURABILITY_MISSING' and isinstance(lineno, int):
            print(f'  - record emit in {fname}() at line {lineno} is outside any work loop')
    print()
    print('  See SKILL.md > "Emit records as work lands" for the correct pattern.')
    sys.exit(1)


if __name__ == '__main__':
    main()
