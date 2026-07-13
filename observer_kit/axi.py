"""Observer Kit AXI — agent-ergonomic CLI surface (TOON stdout).

Principles (subset of https://github.com/kunchenguid/axi):
token-efficient TOON, minimal fields, definitive empty states, structured exit
codes, next-step help[]. Human dashboard remains the visual review surface.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import urlopen


# --- TOON emission (stdlib, no dependency on toonformat package) ---------------

def _toon_scalar(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == '' or any(ch in text for ch in ':\n,"[]{}') or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def toon_kv(key: str, value: Any) -> str:
    return f'{key}: {_toon_scalar(value)}'


def toon_table(name: str, rows: list[dict], columns: list[str]) -> str:
    """Emit TOON-style table: name[N]{cols}: then space-indented CSV-ish rows."""
    if not rows:
        return f'{name}[0]{{{",".join(columns)}}}:'
    lines = [f'{name}[{len(rows)}]{{{",".join(columns)}}}:']
    for row in rows:
        cells = []
        for col in columns:
            cells.append(_toon_scalar(row.get(col)))
        lines.append('  ' + ','.join(cells))
    return '\n'.join(lines)


def toon_help(items: Iterable[str]) -> str:
    items = [str(i) for i in items if i]
    if not items:
        return 'help[0]:'
    # Quoted list like no-mistakes
    body = ','.join(json.dumps(i, ensure_ascii=False) for i in items)
    return f'help[{len(items)}]: {body}'


def emit(*blocks: str) -> None:
    """Write TOON blocks to stdout (agents parse stdout; progress stays stderr)."""
    text = '\n'.join(b for b in blocks if b)
    if text:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()


# --- Discovery helpers --------------------------------------------------------

ACTIVE_S = 120


def _pid_alive(pid: object) -> bool:
    try:
        p = int(pid)  # type: ignore[arg-type]
        if p <= 0:
            return False
        os.kill(p, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _read_jsonl_tail(path: Path, max_lines: int = 80) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _first_run_started(path: Path) -> dict:
    for rec in _read_jsonl_tail(path, max_lines=5000):
        if (rec.get('event') or rec.get('action')) == 'run_started':
            return rec
    # fall back to first line
    rows = _read_jsonl_tail(path, max_lines=5000)
    return rows[0] if rows else {}


def _last_event(path: Path) -> dict:
    rows = _read_jsonl_tail(path, max_lines=40)
    return rows[-1] if rows else {}


def _lock_path_for_events(events_path: Path, state_dir: Path) -> Path | None:
    # Preferred: state/runs/<lane>/events.jsonl → state/<lane>.lock
    try:
        if events_path.name == 'events.jsonl' and events_path.parent.parent.name == 'runs':
            lane = events_path.parent.name
            return state_dir / f'{lane}.lock'
        if events_path.suffix == '.jsonl':
            return state_dir / f'{events_path.stem}.lock'
    except OSError:
        return None
    return None


def _is_live(events_path: Path, state_dir: Path, now: float) -> bool:
    last = _last_event(events_path)
    event = last.get('event') or last.get('action')
    if event in {'run_finished', 'run_failed', 'run_abandoned', 'run_paused'}:
        return False
    lock_path = _lock_path_for_events(events_path, state_dir)
    if lock_path and lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding='utf-8'))
            pid = int(lock.get('pid') or 0)
            if pid <= 0:
                return False
            return _pid_alive(pid)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if last.get('pid') is not None and not _pid_alive(last.get('pid')):
        return False
    try:
        mtime = events_path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) < ACTIVE_S


def _terminal_status(events_path: Path) -> str:
    last = _last_event(events_path)
    event = last.get('event') or last.get('action') or ''
    if event == 'run_finished':
        return str(last.get('status') or 'success')
    if event == 'run_failed':
        return 'failed'
    if event == 'run_abandoned':
        return 'abandoned'
    if event == 'run_paused':
        return 'paused'
    if event == 'run_started':
        return 'started'
    return event or 'unknown'


def _count_records(events_path: Path) -> int:
    n = 0
    if not events_path.is_file():
        return 0
    try:
        with events_path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if '"event": "record"' in line or '"event":"record"' in line:
                    n += 1
    except OSError:
        return 0
    return n


def list_runs(state_dir: Path) -> list[dict]:
    """Minimal run inventory for AXI (not a full dashboard list_runs port)."""
    state_dir = state_dir.expanduser().resolve()
    now = time.time()
    runs: list[dict] = []
    seen: set[str] = set()

    lanes = state_dir / 'runs'
    if lanes.is_dir():
        for lane_dir in sorted(lanes.iterdir()):
            if not lane_dir.is_dir():
                continue
            ev = lane_dir / 'events.jsonl'
            if not ev.is_file():
                continue
            real = str(ev.resolve())
            if real in seen:
                continue
            seen.add(real)
            started = _first_run_started(ev)
            if (started.get('event') or started.get('action')) != 'run_started':
                # still list ledgers with any events
                if not _read_jsonl_tail(ev, 1):
                    continue
            try:
                mtime = ev.stat().st_mtime
            except OSError:
                mtime = 0
            run_id = f'runguard:{lane_dir.name}'
            runs.append({
                'id': run_id,
                'lane': lane_dir.name,
                'live': _is_live(ev, state_dir, now),
                'status': _terminal_status(ev),
                'desc': str(
                    started.get('description')
                    or started.get('name')
                    or lane_dir.name
                )[:80],
                'records': _count_records(ev),
                'mtime': int(mtime),
            })

    # Legacy flat *.jsonl at state root
    if state_dir.is_dir():
        for path in sorted(state_dir.glob('*.jsonl')):
            if path.name in {'chat.jsonl', 'controls.jsonl'}:
                continue
            real = str(path.resolve())
            if real in seen:
                continue
            seen.add(real)
            started = _first_run_started(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            runs.append({
                'id': f'runguard:{path.name}',
                'lane': path.stem,
                'live': _is_live(path, state_dir, now),
                'status': _terminal_status(path),
                'desc': str(
                    started.get('description') or started.get('name') or path.stem
                )[:80],
                'records': _count_records(path),
                'mtime': int(mtime),
            })

    runs.sort(key=lambda r: -int(r.get('mtime') or 0))
    return runs


def get_run(state_dir: Path, run_id: str) -> dict | None:
    for run in list_runs(state_dir):
        if run['id'] == run_id or run['lane'] == run_id:
            return run
        # allow bare lane / with runguard: prefix
        if run_id.startswith('runguard:') and run['id'] == run_id:
            return run
    return None


def probe_dashboard(port: int = 8484) -> dict | None:
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/meta', timeout=0.35) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode('utf-8') or '{}')
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def default_help(state_dir: str, *, live: int = 0, orphans: int = 0) -> list[str]:
    helps = [
        f'observer-kit axi runs --state-dir {state_dir}',
        f'observer-kit dashboard {state_dir}',
        f'observer-kit poll {state_dir} --all',
        f'observer-kit lint <workflow.py>',
    ]
    if orphans:
        helps.insert(0, f'observer-kit stop --sweep {state_dir}')
    if live:
        helps.insert(0, f'observer-kit axi run --state-dir {state_dir} --id <run-id>')
    return helps
