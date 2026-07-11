"""Shared durable runtime for the synthetic Observer Flow examples."""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
NODES_DIR = (HERE / "nodes").resolve()
REUSABLE = {"succeeded", "skipped", "cached"}
CONDITION_OPS = {
    "eq", "ne", "present", "empty", "contains", "gt", "gte", "lt", "lte", "in"
}
RECIPE_STATUSES = {"candidate", "proven", "superseded"}
RowStatus = Callable[[sqlite3.Connection, str], tuple[str, str]]


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    existing_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(node_results)")
    }
    required_columns = {
        "run_id", "table_name", "row_key", "node_id", "node_version",
        "input_hash", "status", "result_json", "fields_json", "evidence_json",
        "reason", "error", "spend_units", "duration_ms", "attempt",
        "created_at", "updated_at",
    }
    if existing_columns and not required_columns.issubset(existing_columns):
        with db:
            db.execute("DROP TABLE node_results")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS rows (
          row_key TEXT PRIMARY KEY,
          data_json TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS node_results (
          run_id TEXT NOT NULL,
          table_name TEXT NOT NULL,
          row_key TEXT NOT NULL,
          node_id TEXT NOT NULL,
          node_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL,
          fields_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          error TEXT NOT NULL,
          spend_units REAL NOT NULL,
          duration_ms INTEGER NOT NULL,
          attempt INTEGER NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (run_id, table_name, row_key, node_id, input_hash)
        );
        CREATE INDEX IF NOT EXISTS node_results_latest
          ON node_results(row_key, node_id, updated_at);
        """
    )
    return db


def condition_value(condition: Optional[dict], row: dict) -> Optional[bool]:
    if not condition:
        return True
    if "all" in condition:
        values = [condition_value(item, row) for item in condition["all"]]
        if any(value is False for value in values):
            return False
        return True if all(value is True for value in values) else None
    if "any" in condition:
        values = [condition_value(item, row) for item in condition["any"]]
        if any(value is True for value in values):
            return True
        return False if all(value is False for value in values) else None
    field, op = condition.get("field"), condition.get("op")
    if field not in row:
        return None
    value, expected = row.get(field), condition.get("value")
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "present":
        return value not in (None, "", [], {})
    if op == "empty":
        return value in (None, "", [], {})
    if op == "contains":
        return str(expected) in str(value)
    try:
        if op == "gt":
            return value > expected
        if op == "gte":
            return value >= expected
        if op == "lt":
            return value < expected
        if op == "lte":
            return value <= expected
        if op == "in":
            return value in expected
    except TypeError as exc:
        raise ValueError(
            f"condition {field!r} {op} has incompatible values: {value!r}, {expected!r}"
        ) from exc
    raise ValueError(f"unsupported demo condition operator: {op}")


def resolve_node_script(node: dict) -> Path:
    script = (HERE / str(node["script"])).resolve()
    try:
        script.relative_to(NODES_DIR)
    except ValueError as exc:
        raise ValueError(
            f"node {node.get('id', '<unknown>')} script must stay under nodes/: "
            f"{node.get('script')!r}"
        ) from exc
    if script.suffix != ".py" or not script.is_file():
        raise ValueError(
            f"node {node.get('id', '<unknown>')} script is not a Python file: "
            f"{node.get('script')!r}"
        )
    return script


def load_node(node: dict):
    script = resolve_node_script(node)
    module_name = ".".join(script.relative_to(HERE).with_suffix("").parts)
    return importlib.import_module(module_name).run


def recipe_identity(node: dict) -> Optional[dict[str, str]]:
    recipe = node.get("recipe")
    if recipe is None:
        return None
    if not isinstance(recipe, dict):
        raise ValueError(f"node {node.get('id', '<unknown>')} recipe must be an object")
    identity = {key: str(recipe.get(key, "")) for key in ("id", "version", "status")}
    if not identity["id"] or not identity["version"] or identity["status"] not in RECIPE_STATUSES:
        raise ValueError(
            f"node {node.get('id', '<unknown>')} recipe requires id, version, and "
            f"status in {sorted(RECIPE_STATUSES)}"
        )
    return identity


def implementation_identity(node: dict) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "script_sha256": hashlib.sha256(resolve_node_script(node).read_bytes()).hexdigest(),
    }
    recipe = recipe_identity(node)
    if recipe:
        identity["recipe"] = recipe
    return identity


def build_plan_id(flow: dict, **runtime: Any) -> str:
    implementations = {
        node["id"]: implementation_identity(node) for node in flow["nodes"]
    }
    return canonical_hash({
        "flow": flow,
        "implementations": implementations,
        "runtime": runtime,
    })


def ordered_nodes(nodes: list[dict]) -> list[dict]:
    pending = {node["id"]: node for node in nodes}
    ordered: list[dict] = []
    complete: set[str] = set()
    while pending:
        ready = [
            node for node in nodes
            if node["id"] in pending and set(node.get("needs", [])).issubset(complete)
        ]
        if not ready:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"flow dependencies cannot be scheduled: {unresolved}")
        for node in ready:
            ordered.append(node)
            complete.add(node["id"])
            pending.pop(node["id"])
    return ordered


def unit_route(node: dict, row: dict, dependency_results: dict[str, dict]) -> tuple[str, str]:
    condition = condition_value(node.get("when"), row)
    if condition is False:
        return "skipped", "branch condition selected another route"
    # Skipped (branch-not-taken) and cached upstream results are usable; only
    # failed/held/missing dependencies block a dependent node.
    unusable = [
        dependency
        for dependency in node.get("needs", [])
        if dependency_results.get(dependency, {}).get("status") not in REUSABLE
    ]
    if unusable or condition is None:
        return "held", "waiting on a usable upstream result"
    return "execute", ""


def node_input_hash(
    node: dict, row: dict, dry_run: bool,
    dependency_results: Optional[dict[str, dict]] = None,
) -> str:
    # Explicit cache-bust fields: a policy/side_effect/outputs change must not
    # reuse a prior succeeded sink result even if version stayed the same.
    # Remaining node keys still flow through config for completeness.
    skip_config = {
        "label", "script", "id", "version", "inputs", "needs", "outputs",
        "policy", "side_effect", "mode", "contract",
    }
    payload = {
        "node_id": node["id"],
        "node_version": str(node.get("version", "1")),
        "implementation": implementation_identity(node),
        "inputs": {field: row.get(field) for field in node.get("inputs", [])},
        "outputs": list(node.get("outputs") or []),
        "policy": node.get("policy"),
        "side_effect": node.get("side_effect"),
        "contract": node.get("contract"),
        "mode": node.get("mode"),
        "upstream_results": {
            dependency: {
                "node_version": (dependency_results or {}).get(dependency, {}).get("node_version"),
                "input_hash": (dependency_results or {}).get(dependency, {}).get("input_hash"),
                "status": (dependency_results or {}).get(dependency, {}).get("status"),
            }
            for dependency in node.get("needs", [])
        },
        "config": {
            key: value for key, value in node.items() if key not in skip_config
        },
    }
    if node.get("mode") == "sink" or node.get("side_effect"):
        payload["execution_mode"] = "dry_run" if dry_run else "full_run"
    return canonical_hash(payload)


# Field/evidence values that claim a real external mutation landed. Dry runs
# that return these are dishonest and are rejected by the central sink gate.
_REAL_WRITE_FIELD_STATUSES = frozenset({
    "written", "appended", "inserted", "upserted", "pushed", "created",
    "updated", "synced", "success", "succeeded", "simulated append",
    "delivered", "committed",
})
_DRY_HONEST_MODES = frozenset({"", "dry_run", "planned", "preview", "simulation"})


def is_sink_node(node: dict) -> bool:
    return node.get("mode") == "sink" or bool(node.get("side_effect"))


def sink_destination(node: dict, run=None) -> str | None:
    side = node.get("side_effect") or {}
    identity = side.get("identity")
    if identity:
        return str(identity)
    if run is not None and getattr(run, "destination", None):
        return str(run.destination)
    return None


def assert_dry_run_honest(result: dict, *, dry_run: bool, node_id: str) -> None:
    """Reject sink results that claim a real mutation during a dry run."""
    if not dry_run:
        return
    evidence = dict(result.get("evidence") or {})
    mode = str(evidence.get("mode") or "").strip().lower()
    confirmation = evidence.get("confirmation")
    if confirmation not in (None, "", False, 0) and mode not in _DRY_HONEST_MODES:
        raise RuntimeError(
            f"dry-run sink {node_id!r} returned a real confirmation "
            f"({confirmation!r}) with mode={mode!r}"
        )
    if mode in {"full_run", "full_run_simulation", "written", "verified"} and confirmation:
        raise RuntimeError(
            f"dry-run sink {node_id!r} claimed full-run delivery (mode={mode!r})"
        )
    for key, value in dict(result.get("fields") or {}).items():
        if not str(key).endswith("_status"):
            continue
        if str(value).strip().lower() in _REAL_WRITE_FIELD_STATUSES:
            raise RuntimeError(
                f"dry-run sink {node_id!r} field {key!r} claims real write "
                f"status {value!r}"
            )


def invoke_node(node_run, node: dict, row: dict, dry_run: bool, run=None,
                row_key: str | None = None) -> dict:
    """Invoke a node with central sink gates (schema, policy, intent, honesty).

    Sinks always receive ``dry_run``. When an ObservedRun is provided:

    - optional node ``contract`` / ``policy`` run before the script
    - ``write_intent`` reserves the destination before the body runs
    - dry-run results that claim real confirmations are rejected
    - ``write_receipt`` records planned or confirmed delivery

    Nodes that mutate outside ``write_intent``/``write_receipt`` still bypass
    receipt durability; the gate makes the cooperative path the default and
    fails closed on dishonest dry-run evidence.
    """
    is_sink = is_sink_node(node)
    if not is_sink:
        return node_run(row)

    key = str(row_key or row.get("domain") or row.get("key") or "row")
    destination = sink_destination(node, run)

    if run is not None:
        contract = node.get("contract")
        if contract:
            if not run.validate(
                row, key=key, contract=contract,
                table=node.get("id", "records"),
                on_error=str(contract.get("on_error") or "pause"),
            ):
                raise RuntimeError(f"schema gate blocked sink {node.get('id')}")
        policy = node.get("policy")
        if policy:
            if not run.allow_write(
                row, key=key, policy=policy, destination=destination,
                on_error=str(policy.get("on_error") or "skip"),
            ):
                raise RuntimeError(f"policy gate blocked sink {node.get('id')}")

    ticket = None
    node_id = str(node.get("id") or "")
    if run is not None and destination:
        ticket = run.write_intent(
            key,
            destination=destination,
            transform_version=str(
                node.get("version")
                or getattr(run, "transform_version", None)
                or "1"
            ),
            payload={field: row.get(field) for field in node.get("inputs", [])},
            # Stamp the ticket so receipts share replay identity with dead_letters
            # that fall back to table=node_id from validate/allow_write.
            node_id=node_id or None,
        )
        # Idempotent prior full write: do not re-enter the sink body.
        if ticket is None and not dry_run:
            return {
                "fields": {},
                "evidence": {
                    "destination": destination,
                    "reason": "idempotent_skip",
                    "mode": "skipped",
                },
                "spend_units": 0,
                "idempotent_skip": True,
            }

    result = node_run(row, dry_run=bool(dry_run))
    assert_dry_run_honest(result, dry_run=bool(dry_run), node_id=node_id or str(node.get("id")))

    if run is not None and ticket is not None:
        # Ledger the planned/confirmed delivery only. Flow already emits the
        # business row via DemoRuntime.emit_record — avoid a second projection.
        evidence = dict(result.get("evidence") or {})
        confirmation = evidence.get("confirmation")
        if confirmation in (None, ""):
            confirmation = None
        run.write_receipt(
            ticket,
            destination_id=confirmation,
            verified=bool(confirmation) and not dry_run,
            # Explicit so receipts match dead_letter(node_id=table) even if an
            # older ticket lacked the stamp.
            node_id=node_id or ticket.get("node_id"),
        )
    return result


def result_map(db: sqlite3.Connection, row_key: str) -> dict[str, dict]:
    rows = db.execute(
        "SELECT * FROM node_results WHERE row_key = ? ORDER BY updated_at, rowid", (row_key,)
    ).fetchall()
    return {row["node_id"]: dict(row) for row in rows}


def matching_result(
    db: sqlite3.Connection, row_key: str, node_id: str, input_hash: str,
) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT rowid AS result_rowid, * FROM node_results
        WHERE row_key = ? AND node_id = ? AND input_hash = ?
        ORDER BY updated_at DESC, rowid DESC LIMIT 1
        """,
        (row_key, node_id, input_hash),
    ).fetchone()


def latest_reusable_result(
    db: sqlite3.Connection, row_key: str, node_id: str,
) -> Optional[sqlite3.Row]:
    """Most recent succeeded/skipped/cached result for a node (any input hash)."""
    return db.execute(
        """
        SELECT rowid AS result_rowid, * FROM node_results
        WHERE row_key = ? AND node_id = ? AND status IN ('succeeded', 'skipped', 'cached')
        ORDER BY updated_at DESC, rowid DESC LIMIT 1
        """,
        (row_key, node_id),
    ).fetchone()


def next_attempt(db: sqlite3.Connection, row_key: str, node_id: str) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(attempt),0) AS attempt FROM node_results WHERE row_key = ? AND node_id = ?",
        (row_key, node_id),
    ).fetchone()
    return int(row["attempt"]) + 1


def load_source_rows(db: sqlite3.Connection, rows: list[dict], key_field: str) -> None:
    """Seed rows from source only. Do not merge into prior attempt's accumulated fields.

    Merging source into stale node outputs produced phantom "complete" rows that
    mixed old derived fields with new inputs. Node outputs are re-applied later
    via cache restore or fresh execution. Drop orphaned node_results (and
    batch_calls when present) when the source shrinks so resume views stay in sync.
    """
    active_keys = [str(row[key_field]) for row in rows]
    placeholders = ",".join("?" for _ in active_keys)
    now = time.time()
    with db:
        db.execute(f"DELETE FROM rows WHERE row_key NOT IN ({placeholders})", active_keys)
        db.execute(
            f"DELETE FROM node_results WHERE row_key NOT IN ({placeholders})", active_keys,
        )
        # batch_calls is optional (batch coordinator only).
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "batch_calls" in tables and active_keys:
            # Keep batch history, but drop planned batches whose members are all gone.
            for batch in db.execute(
                "SELECT batch_id, row_keys_json FROM batch_calls"
            ).fetchall():
                try:
                    keys = json.loads(batch["row_keys_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    keys = []
                if keys and not any(str(k) in set(active_keys) for k in keys):
                    db.execute(
                        "DELETE FROM batch_calls WHERE batch_id = ?",
                        (batch["batch_id"],),
                    )
        for source_row in rows:
            row_key = str(source_row[key_field])
            # Source snapshot only — never carry prior derived fields forward.
            current = dict(source_row)
            db.execute(
                """
                INSERT INTO rows(row_key,data_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(row_key) DO UPDATE SET
                  data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (row_key, json.dumps(current, sort_keys=True), now),
            )


def restore_cached_result(db: sqlite3.Connection, row_key: str, result) -> None:
    current = row_data(db, row_key)
    current.update(json.loads(result["fields_json"]))
    now = time.time()
    with db:
        db.execute(
            "UPDATE rows SET data_json = ?, updated_at = ? WHERE row_key = ?",
            (json.dumps(current, sort_keys=True), now, row_key),
        )
        db.execute(
            "UPDATE node_results SET updated_at = ? WHERE rowid = ?",
            (now, result["result_rowid"]),
        )


def row_data(db: sqlite3.Connection, row_key: str) -> dict:
    row = db.execute("SELECT data_json FROM rows WHERE row_key = ?", (row_key,)).fetchone()
    return json.loads(row["data_json"]) if row else {}


def flow_snapshot(db: sqlite3.Connection, row_key: str, nodes: list[dict]) -> dict:
    results = result_map(db, row_key)
    return {
        node["id"]: {
            "status": results.get(node["id"], {}).get("status", "pending"),
            "version": results.get(node["id"], {}).get("node_version", node.get("version", "1")),
            "input_hash": results.get(node["id"], {}).get("input_hash", ""),
            "attempt": results.get(node["id"], {}).get("attempt", 0),
            "reason": results.get(node["id"], {}).get("reason", ""),
            "duration_ms": results.get(node["id"], {}).get("duration_ms", 0),
            "spend_units": results.get(node["id"], {}).get("spend_units", 0),
        }
        for node in nodes
    }


def persist_result(
    db: sqlite3.Connection,
    row_key: str,
    node_id: str,
    status: str,
    fields: dict,
    evidence: dict,
    reason: str,
    error: str,
    spend_units: float,
    duration_ms: int,
    *,
    run_id: str,
    table_name: str,
    node_version: str,
    input_hash: str,
    attempt: int,
) -> None:
    current = row_data(db, row_key)
    current.update(fields)
    now = time.time()
    result = {
        "status": status,
        "fields": fields,
        "evidence": evidence,
        "reason": reason,
        "error": error,
        "spend_units": spend_units,
        "duration_ms": duration_ms,
    }
    with db:
        db.execute(
            """
            INSERT INTO node_results
              (run_id,table_name,row_key,node_id,node_version,input_hash,status,result_json,
               fields_json,evidence_json,reason,error,spend_units,duration_ms,attempt,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,table_name,row_key,node_id,input_hash) DO UPDATE SET
              node_version=excluded.node_version, status=excluded.status,
              result_json=excluded.result_json, fields_json=excluded.fields_json,
              evidence_json=excluded.evidence_json, reason=excluded.reason,
              error=excluded.error, spend_units=excluded.spend_units,
              duration_ms=excluded.duration_ms, attempt=excluded.attempt,
              updated_at=excluded.updated_at
            """,
            (
                run_id, table_name, row_key, node_id, node_version, input_hash, status,
                json.dumps(result, sort_keys=True), json.dumps(fields, sort_keys=True),
                json.dumps(evidence, sort_keys=True), reason, error, spend_units,
                duration_ms, attempt, now, now,
            ),
        )
        db.execute(
            "UPDATE rows SET data_json = ?, updated_at = ? WHERE row_key = ?",
            (json.dumps(current, sort_keys=True), now, row_key),
        )


def emit(ledger, run, event: str, **fields) -> None:
    ledger(run.scope, event, attempt=run.attempt, dry_run=run.dry_run, **fields)


def aggregate_node_status(counts: dict[str, int], total: int, requested: str) -> str:
    if requested == "running" or sum(counts.values()) < total:
        return "running"
    if counts.get("failed"):
        return "failed"
    if counts.get("held"):
        return "held"
    return "complete"


def latest_node_results(db: sqlite3.Connection, node_id: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in db.execute(
        """
        SELECT node_results.* FROM node_results
        JOIN rows ON rows.row_key = node_results.row_key
        WHERE node_results.node_id = ?
        ORDER BY node_results.updated_at, node_results.rowid
        """,
        (node_id,),
    ):
        latest[row["row_key"]] = dict(row)
    return list(latest.values())


def graph_event(flow: dict, plan_id: str, rows_total: int) -> dict:
    nodes = []
    edges = []
    for node in flow["nodes"]:
        nodes.append({
            key: node[key]
            for key in (
                "id", "label", "version", "kind", "mode", "script", "recipe",
                "needs", "inputs", "outputs", "when", "edge_label", "batch",
            )
            if key in node
        })
        for dependency in node.get("needs", []):
            edges.append({
                "from": dependency,
                "to": node["id"],
                "label": node.get("edge_label", "then"),
            })
    return {
        "id": flow["graph"]["id"],
        "label": flow["graph"].get("label"),
        "description": flow["graph"].get("description"),
        "version": flow["graph"]["version"],
        "table": flow["source"]["table"],
        "plan_id": plan_id,
        "nodes": nodes,
        "edges": edges,
        "rows_total": rows_total,
    }


class DemoRuntime:
    """Project durable node results into one synthetic dashboard table."""

    def __init__(
        self,
        ledger,
        run,
        db: sqlite3.Connection,
        nodes: list[dict],
        *,
        table: str,
        hidden_fields: set[str],
        row_status: RowStatus,
    ) -> None:
        self.ledger = ledger
        self.run = run
        self.db = db
        self.nodes = nodes
        self.table = table
        self.hidden_fields = hidden_fields
        self.row_status = row_status

    def emit_record(self, row_key: str, node: dict) -> None:
        data = row_data(self.db, row_key)
        status, error = self.row_status(self.db, row_key)
        projection = {
            key: value for key, value in data.items() if key not in self.hidden_fields
        }
        emit(
            self.ledger,
            self.run,
            "record",
            table=self.table,
            key=row_key,
            current_node=node.get("label", node["id"]),
            flow_status=status,
            flow_json=flow_snapshot(self.db, row_key, self.nodes),
            error=error,
            **projection,
        )

    def node_counts(self, node_id: str) -> dict[str, int]:
        counts = {name: 0 for name in ("succeeded", "skipped", "held", "failed", "cached")}
        for row in latest_node_results(self.db, node_id):
            if row["status"] in counts:
                counts[row["status"]] += 1
        return counts

    def emit_node(self, node: dict, total: int, status: str, **extra: Any) -> None:
        counts = self.node_counts(node["id"])
        current_results = latest_node_results(self.db, node["id"])
        spend = sum(float(row["spend_units"]) for row in current_results)
        emit(
            self.ledger,
            self.run,
            "flow_node",
            node_id=node["id"],
            node_label=node.get("label", node["id"]),
            status=aggregate_node_status(counts, total, status),
            total=total,
            completed=sum(counts.values()),
            spend_units=round(float(spend), 4),
            **counts,
            **extra,
        )

    def persist_terminal(
        self,
        row_key: str,
        node: dict,
        *,
        status: str,
        fields: Optional[dict] = None,
        evidence: Optional[dict] = None,
        reason: str,
        error: str = "",
        spend_units: float = 0,
        duration_ms: int = 1,
        batch_id: str = "",
        input_hash: str = "",
        attempt: int = 0,
    ) -> None:
        input_hash = input_hash or node_input_hash(
            node, row_data(self.db, row_key), self.run.dry_run,
            result_map(self.db, row_key),
        )
        attempt = attempt or next_attempt(self.db, row_key, node["id"])
        persist_result(
            self.db,
            row_key,
            node["id"],
            status,
            fields or {},
            evidence or {},
            reason,
            error,
            spend_units,
            duration_ms,
            run_id=self.run.scope,
            table_name=self.table,
            node_version=str(node.get("version", "1")),
            input_hash=input_hash,
            attempt=attempt,
        )
        event = {
            "node_id": node["id"],
            "node_label": node.get("label", node["id"]),
            "table": self.table,
            "key": row_key,
            "status": status,
            "reason": reason,
            "error": error,
            "node_version": str(node.get("version", "1")),
            "input_hash": input_hash,
            "unit_attempt": attempt,
            "duration_ms": duration_ms,
            "spend_units": spend_units,
        }
        if batch_id:
            event["batch_id"] = batch_id
        emit(self.ledger, self.run, "flow_unit", **event)
        self.emit_record(row_key, node)

    def persist_and_emit_row(
        self,
        row_key: str,
        node: dict,
        node_run,
        *,
        provider_rate: float,
        delay: float,
        position: Optional[int] = None,
        total: Optional[int] = None,
    ) -> str:
        current = row_data(self.db, row_key)
        dependencies = result_map(self.db, row_key)
        input_hash = node_input_hash(node, current, self.run.dry_run, dependencies)
        prior = matching_result(self.db, row_key, node["id"], input_hash)
        if prior and prior["status"] in REUSABLE:
            restore_cached_result(self.db, row_key, prior)
            # Refresh this run's durable row without changing the semantic status
            # (succeeded/skipped). Rewriting status to "cached" would alter
            # downstream input hashes and break branch/sink reuse.
            try:
                prior_fields = json.loads(prior["fields_json"] or "{}")
                prior_evidence = json.loads(prior["evidence_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_fields, prior_evidence = {}, {}
            persist_result(
                self.db,
                row_key,
                node["id"],
                str(prior["status"]),
                prior_fields,
                prior_evidence,
                prior["reason"] or "reused durable node result",
                prior["error"] or "",
                float(prior["spend_units"] or 0),
                int(prior["duration_ms"] or 1),
                run_id=self.run.scope,
                table_name=self.table,
                node_version=str(prior["node_version"] or node.get("version", "1")),
                input_hash=input_hash,
                attempt=int(prior["attempt"] or 1),
            )
            emit(
                self.ledger,
                self.run,
                "flow_unit",
                node_id=node["id"],
                node_label=node.get("label", node["id"]),
                table=self.table,
                key=row_key,
                status="cached",
                reason="reused durable node result",
                node_version=prior["node_version"],
                input_hash=input_hash,
                unit_attempt=prior["attempt"],
                duration_ms=prior["duration_ms"],
                spend_units=0,
            )
            self.emit_record(row_key, node)
            # Cache hits are still durable row boundaries: honor pause/stop here
            # so dry→full reuse (the common path) cannot skip operator controls.
            self.run.checkpoint(node["id"], row_key)
            self.run.check_controls(after_record=True)
            return str(prior["status"])

        # max_attempts is a ceiling on failed tries for the same input hash.
        # Reusable successes remain cache hits above; this only blocks re-execution
        # after the failure budget is spent (does not block dry→full success cache).
        max_attempts = int(node.get("max_attempts") or 0)
        if (
            prior is not None
            and prior["status"] == "failed"
            and max_attempts > 0
            and int(prior["attempt"] or 0) >= max_attempts
        ):
            self.persist_terminal(
                row_key,
                node,
                status="failed",
                reason=f"max_attempts ({max_attempts}) exhausted for this input",
                error=prior["error"] or f"max_attempts {max_attempts} exhausted",
                input_hash=input_hash,
                attempt=int(prior["attempt"] or max_attempts),
            )
            self.run.checkpoint(node["id"], row_key)
            self.run.check_controls(after_record=True)
            return "failed"

        route, reason = unit_route(node, current, dependencies)
        running = {
            "node_id": node["id"],
            "node_label": node.get("label", node["id"]),
            "table": self.table,
            "key": row_key,
            "status": "running",
        }
        if position is not None:
            running["position"] = position
        if total is not None:
            running["total"] = total
        emit(self.ledger, self.run, "flow_unit", **running)

        started = time.monotonic()
        attempt = next_attempt(self.db, row_key, node["id"])
        fields: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        error = ""
        spend_units = 0.0
        if route in {"skipped", "held"}:
            status = route
        else:
            try:
                spend = node.get("spend")
                if spend:
                    from runguard import throttle
                    throttle(f"demo-{spend['provider']}", provider_rate)
                result = invoke_node(
                    node_run, node, current, self.run.dry_run,
                    run=self.run, row_key=row_key,
                )
                fields = dict(result.get("fields", {}))
                evidence = dict(result.get("evidence", {}))
                spend_units = float(result.get("spend_units", 0))
                if result.get("idempotent_skip"):
                    # Receipt already proves delivery; re-materialize display
                    # fields from the last durable result when present.
                    prior_fields = latest_reusable_result(
                        self.db, row_key, node["id"],
                    )
                    if prior_fields is not None:
                        try:
                            fields = json.loads(prior_fields["fields_json"] or "{}")
                            evidence = {
                                **json.loads(prior_fields["evidence_json"] or "{}"),
                                "reason": "idempotent_skip",
                                "mode": "skipped",
                            }
                        except (TypeError, json.JSONDecodeError):
                            pass
                    status = "skipped"
                    reason = "idempotent sink write already receipted"
                else:
                    status = "succeeded"
                    reason = "durable node result committed"
            except Exception as exc:
                status = "failed"
                reason = "node execution failed"
                error = str(exc)
                spend_units = float(node.get("spend", {}).get("units_per_call", 0))
        duration_ms = max(1, int((time.monotonic() - started) * 1000))
        self.persist_terminal(
            row_key,
            node,
            status=status,
            fields=fields,
            evidence=evidence,
            reason=reason,
            error=error,
            spend_units=spend_units,
            duration_ms=duration_ms,
            input_hash=input_hash,
            attempt=attempt,
        )
        self.run.checkpoint(node["id"], row_key)
        self.run.check_controls(after_record=True)
        time.sleep(max(0.0, delay))
        return status
