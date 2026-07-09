#!/usr/bin/env python3
"""Acceptance tests for run_dashboard.py JSONL reading behavior."""
import importlib.util
import json
import os
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DASHBOARD = os.path.join(HERE, 'run_dashboard.py')
passed, failed = 0, 0


def ok(name, cond, detail=''):
    global passed, failed
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  — {detail}" if detail and not cond else ''))
    if cond:
        passed += 1
    else:
        failed += 1


spec = importlib.util.spec_from_file_location('run_dashboard_under_test', RUN_DASHBOARD)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

print(f"Testing run_dashboard.py at {RUN_DASHBOARD}\n")

with tempfile.TemporaryDirectory(prefix='rgdash-') as state:
    dashboard.SOURCES['runguard'] = state
    dashboard.SOURCES['push'] = os.path.join(state, 'missing-push')
    dashboard.SOURCES['enrich'] = os.path.join(state, 'missing-enrich')
    dashboard.EVENT_READ_BYTES = 80
    ledger = os.path.join(state, 'large-run.jsonl')
    rows = [
        {'ts': '2026-07-09T12:00:00', 'event': 'run_started', 'description': 'old dry-run attempt', 'todo': 1},
        {'ts': '2026-07-09T12:00:01', 'event': 'record', 'table': 'companies', 'key': 'old'},
        {'ts': '2026-07-09T12:01:00', 'event': 'run_started', 'description': 'latest large dashboard check', 'todo': 2},
        {
            'ts': '2026-07-09T12:00:01',
            'event': 'record',
            'table': 'companies',
            'key': 'big',
            'company': 'big.example',
            'notes': 'x' * 500,
        },
        {'ts': '2026-07-09T12:00:02', 'event': 'record', 'table': 'companies', 'key': 'small'},
    ]
    with open(ledger, 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row) + '\n')

    offsets = {}
    seen = []
    for _ in range(10):
        events, offsets = dashboard.read_events('runguard:large-run.jsonl', offsets)
        seen.extend(e.get('key') for e in events if e.get('event') == 'record')
        if list(offsets.values())[0] == os.path.getsize(ledger):
            break
    ok("chunked read keeps full JSONL records", seen == ['old', 'big', 'small'], str(seen))
    ok("chunked read advances beyond the large record", list(offsets.values())[0] > 80)
    ok("chunked reads eventually reach end", list(offsets.values())[0] == os.path.getsize(ledger))

    runs = dashboard.list_runs()
    desc = next((r.get('desc') for r in runs if r.get('label') == 'large-run.jsonl'), '')
    ok("run list describes the latest attempt", desc == 'latest large dashboard check', desc)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
