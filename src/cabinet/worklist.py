"""SQLite-backed worklist.

State that persists across cabinet phases. Mirrors the homing pattern: each
unit (folder, file, duplicate-pair) walks through statuses
(discovered → scanned → classified → triaged → approved → applied | undone)
and accumulates findings + decisions + events.

All SQL is parameterized. Schema is created on first open.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

UNIT_KINDS: frozenset[str] = frozenset({"folder", "file", "duplicate-pair"})

STATUSES: tuple[str, ...] = (
    "discovered",
    "scanned",
    "classified",
    "triaged",
    "approved",
    "applied",
    "undone",
    "needs-human",
)
_STATUS_SET = frozenset(STATUSES)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(kind, path)
);

CREATE INDEX IF NOT EXISTS idx_units_status ON units(status);
CREATE INDEX IF NOT EXISTS idx_units_kind ON units(kind);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    rule TEXT NOT NULL,
    confidence REAL NOT NULL,
    classifications_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    source_paths_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    FOREIGN KEY (unit_id) REFERENCES units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_findings_unit ON findings(unit_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    FOREIGN KEY (unit_id) REFERENCES units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_decisions_unit ON decisions(unit_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    unit_id INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL,
    FOREIGN KEY (unit_id) REFERENCES units(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_unit ON events(unit_id);
"""


@dataclass(frozen=True, slots=True)
class Unit:
    id: int
    kind: str
    path: str
    status: str
    metadata: dict[str, Any]
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Finding:
    id: int
    unit_id: int
    rule: str
    confidence: float
    classifications: list[str]
    evidence: dict[str, Any]
    source_paths: list[str]
    created_at: float


@dataclass(frozen=True, slots=True)
class Decision:
    id: int
    unit_id: int
    action: str
    payload: dict[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    phase: str
    started_at: float
    ended_at: float | None
    summary: dict[str, Any]


def _now() -> float:
    return time.time()


def _validate_kind(kind: str) -> None:
    if kind not in UNIT_KINDS:
        raise ValueError(f"invalid unit kind {kind!r}; expected one of {sorted(UNIT_KINDS)}")


def _validate_status(status: str) -> None:
    if status not in _STATUS_SET:
        raise ValueError(f"invalid status {status!r}; expected one of {STATUSES}")


class Worklist:
    """SQLite-backed worklist with a small, focused public API."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ------------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------------ units

    def add_unit(
        self,
        kind: str,
        path: str,
        *,
        status: str = "discovered",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert or update (upsert) a unit. Returns the unit id."""
        _validate_kind(kind)
        _validate_status(status)
        meta = metadata or {}
        meta_json = json.dumps(meta, sort_keys=True)
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO units(kind, path, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, path) DO UPDATE SET
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (kind, path, status, meta_json, now, now),
            )
            # Fetch the id either way (RETURNING is sqlite >=3.35; supported on py3.11+).
            row = self._conn.execute(
                "SELECT id FROM units WHERE kind = ? AND path = ?", (kind, path)
            ).fetchone()
            return int(row[0])

    def update_status(self, unit_id: int, status: str) -> None:
        _validate_status(status)
        with self._conn:
            self._conn.execute(
                "UPDATE units SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), unit_id),
            )

    def unit(self, unit_id: int) -> Unit | None:
        row = self._conn.execute(
            "SELECT id, kind, path, status, metadata_json, created_at, updated_at "
            "FROM units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        return _row_to_unit(row) if row else None

    def unit_by_path(self, kind: str, path: str) -> Unit | None:
        _validate_kind(kind)
        row = self._conn.execute(
            "SELECT id, kind, path, status, metadata_json, created_at, updated_at "
            "FROM units WHERE kind = ? AND path = ?",
            (kind, path),
        ).fetchone()
        return _row_to_unit(row) if row else None

    def units_by_status(self, status: str) -> list[Unit]:
        _validate_status(status)
        rows = self._conn.execute(
            "SELECT id, kind, path, status, metadata_json, created_at, updated_at "
            "FROM units WHERE status = ? ORDER BY path",
            (status,),
        ).fetchall()
        return [_row_to_unit(r) for r in rows]

    def all_units(self) -> list[Unit]:
        rows = self._conn.execute(
            "SELECT id, kind, path, status, metadata_json, created_at, updated_at "
            "FROM units ORDER BY path"
        ).fetchall()
        return [_row_to_unit(r) for r in rows]

    # --------------------------------------------------------------- findings

    def record_finding(
        self,
        unit_id: int,
        rule: str,
        *,
        confidence: float,
        classifications: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
        source_paths: list[str] | None = None,
    ) -> int:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1]; got {confidence}")
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO findings(
                    unit_id, rule, confidence,
                    classifications_json, evidence_json, source_paths_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    rule,
                    confidence,
                    json.dumps(classifications or [], sort_keys=True),
                    json.dumps(evidence or {}, sort_keys=True),
                    json.dumps(source_paths or [], sort_keys=True),
                    _now(),
                ),
            )
            assert cur.lastrowid is not None, "INSERT did not produce lastrowid"
            return int(cur.lastrowid)

    def findings_for(self, unit_id: int) -> list[Finding]:
        rows = self._conn.execute(
            "SELECT id, unit_id, rule, confidence, classifications_json, "
            "evidence_json, source_paths_json, created_at "
            "FROM findings WHERE unit_id = ? ORDER BY id",
            (unit_id,),
        ).fetchall()
        return [_row_to_finding(r) for r in rows]

    # -------------------------------------------------------------- decisions

    def record_decision(
        self,
        unit_id: int,
        action: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO decisions(unit_id, action, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (unit_id, action, json.dumps(payload or {}, sort_keys=True), _now()),
            )
            assert cur.lastrowid is not None, "INSERT did not produce lastrowid"
            return int(cur.lastrowid)

    def decisions_for(self, unit_id: int) -> list[Decision]:
        rows = self._conn.execute(
            "SELECT id, unit_id, action, payload_json, created_at "
            "FROM decisions WHERE unit_id = ? ORDER BY id",
            (unit_id,),
        ).fetchall()
        return [_row_to_decision(r) for r in rows]

    # ------------------------------------------------------------------- runs

    def start_run(self, phase: str) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs(phase, started_at, summary_json) VALUES (?, ?, ?)",
                (phase, _now(), "{}"),
            )
            assert cur.lastrowid is not None, "INSERT did not produce lastrowid"
            return int(cur.lastrowid)

    def end_run(self, run_id: int, *, summary: dict[str, Any] | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET ended_at = ?, summary_json = ? WHERE id = ?",
                (_now(), json.dumps(summary or {}, sort_keys=True), run_id),
            )

    def runs(self) -> list[Run]:
        rows = self._conn.execute(
            "SELECT id, phase, started_at, ended_at, summary_json "
            "FROM runs ORDER BY id"
        ).fetchall()
        return [
            Run(
                id=r[0],
                phase=r[1],
                started_at=r[2],
                ended_at=r[3],
                summary=json.loads(r[4]) if r[4] else {},
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- events

    def event(
        self,
        kind: str,
        *,
        run_id: int | None = None,
        unit_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO events(run_id, unit_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, unit_id, kind, json.dumps(payload or {}, sort_keys=True), _now()),
            )
            assert cur.lastrowid is not None, "INSERT did not produce lastrowid"
            return int(cur.lastrowid)

    def events_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, run_id, unit_id, kind, payload_json, created_at "
            "FROM events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "run_id": r[1],
                "unit_id": r[2],
                "kind": r[3],
                "payload": json.loads(r[4]) if r[4] else {},
                "created_at": r[5],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------- closing

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Worklist":
        return self


    # ------------------------------------------------------------------
    # Bridge methods — adapt the worklist to triage / reconcile / planner
    # consumer-facing types. Lazily import the consumer dataclasses to
    # avoid circular imports. Tolerate missing / weird data — these are
    # the "flexibility" surfaces.
    # ------------------------------------------------------------------

    def get_triage_units(self) -> list:
        """Return TriageUnit objects, one per unit, ready for triage rendering.

        Tolerates: units with no findings (classification='unknown'), findings
        with weird evidence shapes (list-of-dicts, single dict, missing fields),
        units with empty metadata.
        """
        from cabinet.triage import TriageUnit  # lazy import

        units = self.all_units()
        out: list = []
        # Build dedup_groups: map content_hash -> [unit paths] for files
        dedup_groups: dict[str, list[str]] = {}
        for u in units:
            if u.kind == "file":
                h = (u.metadata or {}).get("content_hash")
                if h and not str(h).startswith("size-only:"):
                    dedup_groups.setdefault(h, []).append(u.path)

        for u in units:
            findings = self.findings_for(u.id)
            best = max(findings, key=lambda f: f.confidence) if findings else None

            if best is not None and best.classifications:
                classification = (
                    best.classifications[0]
                    if isinstance(best.classifications, list)
                    else str(best.classifications)
                )
                confidence = float(best.confidence)
                evidence_source = best.rule
                evidence_notes = _stringify_evidence(best.evidence)
            else:
                classification = "unknown"
                confidence = 0.0
                evidence_source = "no-rule-fired" if findings is not None else "not-classified"
                evidence_notes = [
                    "No deterministic rule matched. "
                    "Set ANTHROPIC_API_KEY and re-run 'cabinet classify' for LLM cascade, "
                    "or mark this unit 'review later' in the triage manifest."
                ]

            meta = u.metadata or {}
            file_count = int(meta.get("file_count") or (1 if u.kind == "file" else 0))
            total_size = int(meta.get("total_size") or meta.get("size") or 0)
            date_range = meta.get("date_range")
            if isinstance(date_range, list) and len(date_range) == 2:
                date_range = (float(date_range[0]), float(date_range[1]))
            elif not (isinstance(date_range, tuple) and len(date_range) == 2):
                date_range = None

            duplicate_group = None
            if u.kind == "file":
                h = meta.get("content_hash")
                if h and not str(h).startswith("size-only:") and len(dedup_groups.get(h, [])) > 1:
                    duplicate_group = f"hash-{str(h)[:12]}"

            out.append(TriageUnit(
                unit_id=str(u.id),
                path=u.path,
                kind=u.kind,
                classification=classification,
                confidence=confidence,
                evidence_source=evidence_source,
                evidence_notes=tuple(evidence_notes),
                file_count=file_count,
                total_size=total_size,
                date_range=date_range,
                suggested_action="keep",
                suggested_archive_dest=None,
                duplicate_group=duplicate_group,
            ))
        return out

    def write_decisions(self, decisions) -> int:
        """Persist a list of reconcile.Decision objects. Returns count written.

        If a decision references a unit_path the worklist doesn't know about,
        records an event and skips (does not crash).
        """
        written = 0
        for d in decisions:
            unit = (
                self.unit_by_path("folder", d.unit_path)
                or self.unit_by_path("file", d.unit_path)
                or self.unit_by_path("duplicate-pair", d.unit_path)
            )
            if unit is None:
                self.event(
                    "decision-orphan",
                    payload={"path": d.unit_path, "action": d.action},
                )
                continue
            payload = {"target": getattr(d, "target", None)}
            if getattr(d, "dedupe_group", None):
                payload["dedupe_group"] = d.dedupe_group
            self.record_decision(unit.id, d.action, payload=payload)
            written += 1
        return written

    def get_decisions(self) -> list:
        """Return reconcile.Decision objects from the decisions table.

        Joins to units to recover the unit_path. Skips orphan decisions
        (decisions whose unit row was deleted) gracefully.
        """
        from cabinet.reconcile import Decision as RecDecision  # lazy

        rows = self._conn.execute(
            """
            SELECT u.path, d.action, d.payload_json
            FROM decisions d
            JOIN units u ON u.id = d.unit_id
            ORDER BY d.created_at, d.id
            """
        ).fetchall()
        out: list = []
        for path, action, payload_json in rows:
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except json.JSONDecodeError:
                payload = {}
            out.append(RecDecision(
                unit_path=path,
                action=action,
                target=payload.get("target"),
                dedupe_group=payload.get("dedupe_group"),
            ))
        return out

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _row_to_unit(row: tuple) -> Unit:
    return Unit(
        id=row[0],
        kind=row[1],
        path=row[2],
        status=row[3],
        metadata=json.loads(row[4]) if row[4] else {},
        created_at=row[5],
        updated_at=row[6],
    )


def _row_to_finding(row: tuple) -> Finding:
    return Finding(
        id=row[0],
        unit_id=row[1],
        rule=row[2],
        confidence=row[3],
        classifications=json.loads(row[4]) if row[4] else [],
        evidence=json.loads(row[5]) if row[5] else {},
        source_paths=json.loads(row[6]) if row[6] else [],
        created_at=row[7],
    )


def _row_to_decision(row: tuple) -> Decision:
    return Decision(
        id=row[0],
        unit_id=row[1],
        action=row[2],
        payload=json.loads(row[3]) if row[3] else {},
        created_at=row[4],
    )


def _stringify_evidence(evidence) -> list[str]:
    """Flatten any reasonable evidence shape into a list of human-readable strings."""
    if not evidence:
        return []
    if isinstance(evidence, list):
        out = []
        for item in evidence:
            if isinstance(item, dict):
                p = item.get("path", "?")
                r = item.get("reason") or item.get("note") or item.get("why") or "(no reason)"
                out.append(f"{p}: {r}")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append(f"{item[0]}: {item[1]}")
            else:
                out.append(str(item))
        return out
    if isinstance(evidence, dict):
        if "items" in evidence and isinstance(evidence["items"], list):
            return _stringify_evidence(evidence["items"])
        return [f"{k}: {v}" for k, v in evidence.items()]
    return [str(evidence)]
