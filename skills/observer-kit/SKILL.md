---
name: observer-kit
description: >-
  Guardrails and a live localhost dashboard for any script that spends API
  credits or mutates shared state (CRM, database, spreadsheets). Use BEFORE
  writing or running batch jobs, enrichment or scraping runs, or bulk record
  writes — including scripts the engineer already wrote. Adds crash-safe run
  locks (a second accidental run refuses to start, so nothing double-spends or
  corrupts data), cross-process rate limiting, an append-only audit ledger, and
  a read-only web view of what each run is doing in real time. Optionally: a
  plain-English EXPLAIN.md the operator can verify, and a sample-first loop with
  inline chat to steer before the full run.
metadata:
  author: edsmkt
  tags: [batch, enrichment, safety, locks, rate-limiting, observability, credits]
---

# observer-kit

Take a batch script — one the engineer already wrote, or one you're about to
write — and make it **guarded and visible**: it can't collide with another run,
every action lands in an audit trail, and the operator watches it live in a
browser. Three stdlib-only files, zero dependencies.

## When to reach for this

The moment a script will:
- call a paid API in bulk (per-lookup credits),
- write in bulk to a CRM, database, or spreadsheet,
- or is one where two accidental concurrent runs would double-spend or corrupt data.

Wire it in **before** the code spends or writes — the guardrails only help if
they're in place first.

## Make an existing script show live + guarded (the core — 3 moves)

Copy `runguard.py` and `run_dashboard.py` into the target project (vendor them;
don't import from the skill dir). Then add three things to the script that
already exists:

**1. Lock it** — one line before the first spend/write:
```python
from runguard import acquire_lock, ledger
acquire_lock('my-scope')   # scope = the dataset's identity, e.g. f'enrich-{table}'
```
A second run on the same scope hard-refuses (SystemExit) while the first is
alive — nothing double-spends. Different scopes still run in parallel.

**2. Ledger it** — bracket the run, and log one `record` per unit of work:
```python
ledger('my-scope', 'run_started', description='what this run does')
for item in work:
    ...whatever the script already does...
    ledger('my-scope', 'record', table='companies', key=item.id,
           company=item.domain, found=n, source='blitz', ok=True)
ledger('my-scope', 'run_finished', processed=len(work))
```
The ledger is BOTH the audit trail and the dashboard's feed. `record` events are
the general path — the dashboard builds a table from whatever fields you log, no
column config. Group steps with `table=` (each becomes its own sub-tab:
companies → contacts → enriched); identify rows with `key=` (repeat a key to
update that row in place → renders `· was X`); every other field becomes a
column; booleans show ✓/—; the top counters are derived from the data.

**3. Watch it** — point the dashboard at the ledger dir and open it:
```bash
python3 run_dashboard.py     # http://localhost:8484
```
Set its `SOURCES` to the project's ledger directory once. Read-only — it tails
the files and never touches a run.

That's the whole core. The script now can't collide, has an audit trail, and
streams live — without changing what it actually does.

## Optional add-ons (reach for the ones that fit)

- **Throttle a shared API** — `throttle('<provider>', <per_second>)` before each
  call, so several parallel runs share ONE rate budget (limits are per account).
- **State intent up front (`EXPLAIN.md`)** — write a plain-English + one-ASCII-diagram
  file in the state dir describing what the run will do and won't. The dashboard's
  "How it works" tab renders it so a non-technical operator can verify before any
  spend. Regenerate it when the pipeline changes. Template bundled.
- **Sample-first — for anything that spends credits.** Run a small sample, call
  `wait_for_feedback(run_id)` (it blocks while the operator reviews the sample in
  the dashboard and leaves notes on specific cells), adjust the script, re-sample,
  then run the full list. Iterations show before/after (`· was X`). Reply and
  resolve notes with `post_chat(run_id, anchor, text, resolved=True)` (badge → ✓).
  A running process can't change its own code, so all real iteration happens on
  the sample; the full run polls `read_chat()` between rounds only for a STOP.
- **Continuous vs separate lanes** — same scope name = one continuous run
  (before/after and chat persist across re-runs). Set `RUNGUARD_SESSION=<slug>`
  to open a separate lane (a dated weekly run, or a clean A/B).

## Safety rules (do not skip)

- A lock refusal is the guard working, not an error to bypass. Stop the named
  PID deliberately; never launch a parallel run to "fix" a stuck one.
- Design so there is no cleanup step: write results to the durable store as they
  land, and resume by re-reading what's still missing. A crash then costs
  nothing and re-running is always safe.
- Put a hard spend ceiling in code, and never re-buy a record whose outcome is
  already logged.

## Deeper reference (in this directory)

- `README.md` — the full pattern and the `record` convention.
- `BUILD-GUIDE.md` — rebuild the whole stack from scratch, with acceptance tests.
- `example_worker.py` — runnable end-to-end example; run two copies to watch the
  lock refuse the second.
- `sample-ledger.jsonl` — demo data; the dashboard renders it with no run needed.
