"""SQLite-backed worklist that carries state across ``homing`` phases.

Phases run as separate commands but share a single source of truth — the
worklist DB at ``~/system/worklist.sqlite``. Each ``unit`` is one
project or place; each phase appends ``findings``, ``events``, and
``runs`` rows. Status transitions are append-only in spirit (the current
status is overwritten, but the underlying evidence is never destroyed).

All DB operations use parameterised SQL. String concatenation into
queries is forbidden — see the ``test_worklist`` SQL-injection escape
test.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- status enum -----------------------------------------------------------

VALID_STATUSES = (
    "discovered",
    "rules-evaluated",
    "classified",
    "drafted",
    "validated",
    "resolved",
    "needs-human",
)

# --- schema ----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK(kind IN ('project','place')),
    name        TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id         INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    rule            TEXT NOT NULL,
    confidence      REAL NOT NULL,
    classifications TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    exit_code   INTEGER,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     INTEGER REFERENCES units(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_status   ON units(status);
CREATE INDEX IF NOT EXISTS idx_findings_unit  ON findings(unit_id);
CREATE INDEX IF NOT EXISTS idx_events_unit    ON events(unit_id);
"""


def _now() -> str:
    """ISO-8601 UTC timestamp with seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Worklist:
    """Thin wrapper around a single SQLite file.

    Open with a filesystem path or ``":memory:"`` for tests. Tables are
    created idempotently on construction, so calling ``Worklist(path)``
    twice is safe.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(
            self._path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions manually if needed
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    # -- connection management ---------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> "Worklist":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    # -- units -------------------------------------------------------------

    def add_unit(
        self,
        kind: str,
        name: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert a unit. Returns its row id.

        Raises ``ValueError`` if ``kind`` is not 'project' or 'place'.
        """
        if kind not in ("project", "place"):
            raise ValueError(f"kind must be 'project' or 'place', got {kind!r}")
        now = _now()
        payload_json = json.dumps(payload or {}, sort_keys=True)
        cur = self._conn.execute(
            """
            INSERT INTO units (kind, name, path, status, created_at, updated_at, payload)
            VALUES (?, ?, ?, 'discovered', ?, ?, ?)
            """,
            (kind, name, path, now, now, payload_json),
        )
        return int(cur.lastrowid)

    def update_status(self, name: str, status: str) -> None:
        """Set ``status`` for the unit identified by ``name``.

        Raises ``ValueError`` if status is not in :data:`VALID_STATUSES`.
        Raises ``KeyError`` if the unit doesn't exist.
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}; valid: {VALID_STATUSES}")
        now = _now()
        cur = self._conn.execute(
            "UPDATE units SET status = ?, updated_at = ? WHERE name = ?",
            (status, now, name),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no unit named {name!r}")

    def unit(self, name: str) -> dict[str, Any] | None:
        """Return the unit row for ``name`` or ``None`` if absent."""
        row = self._conn.execute(
            "SELECT * FROM units WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_unit(row)

    def units_by_status(self, status: str) -> list[dict[str, Any]]:
        """All units currently in ``status`` (sorted by name)."""
        rows = self._conn.execute(
            "SELECT * FROM units WHERE status = ? ORDER BY name", (status,)
        ).fetchall()
        return [_row_to_unit(r) for r in rows]

    def all_units(self) -> list[dict[str, Any]]:
        """All units regardless of status, sorted by name."""
        rows = self._conn.execute("SELECT * FROM units ORDER BY name").fetchall()
        return [_row_to_unit(r) for r in rows]

    # -- findings ----------------------------------------------------------

    def record_finding(
        self,
        name: str,
        rule: str,
        confidence: float,
        classifications: dict[str, Any],
        evidence: list[Any],
    ) -> int:
        """Append a rule finding for the unit identified by ``name``."""
        unit_id = self._unit_id_or_raise(name)
        cur = self._conn.execute(
            """
            INSERT INTO findings
                (unit_id, rule, confidence, classifications, evidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id,
                rule,
                float(confidence),
                json.dumps(classifications, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                _now(),
            ),
        )
        return int(cur.lastrowid)

    def findings_for(self, name: str) -> list[dict[str, Any]]:
        """Return findings recorded for ``name`` ordered by id (insertion)."""
        unit_id = self._unit_id_or_raise(name)
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE unit_id = ? ORDER BY id", (unit_id,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "unit_id": r["unit_id"],
                    "rule": r["rule"],
                    "confidence": r["confidence"],
                    "classifications": json.loads(r["classifications"]),
                    "evidence": json.loads(r["evidence"]),
                    "created_at": r["created_at"],
                }
            )
        return out

    # -- runs --------------------------------------------------------------

    def start_run(self, command: str) -> int:
        """Open a new run row. Returns its id."""
        cur = self._conn.execute(
            "INSERT INTO runs (command, started_at) VALUES (?, ?)",
            (command, _now()),
        )
        return int(cur.lastrowid)

    def end_run(self, run_id: int, exit_code: int, summary: str) -> None:
        """Close the run row identified by ``run_id``."""
        cur = self._conn.execute(
            """
            UPDATE runs
               SET ended_at = ?, exit_code = ?, summary = ?
             WHERE id = ?
            """,
            (_now(), int(exit_code), summary, run_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no run with id {run_id}")

    # -- events ------------------------------------------------------------

    def event(self, name: str | None, type: str, message: str) -> int:
        """Append an event. ``name`` may be ``None`` for global events."""
        unit_id = self._unit_id_or_raise(name) if name is not None else None
        cur = self._conn.execute(
            """
            INSERT INTO events (unit_id, type, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (unit_id, type, message, _now()),
        )
        return int(cur.lastrowid)

    def events_for(self, name: str) -> list[dict[str, Any]]:
        """Events attached to a specific unit, ordered chronologically."""
        unit_id = self._unit_id_or_raise(name)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE unit_id = ? ORDER BY id", (unit_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- internals ---------------------------------------------------------

    def _unit_id_or_raise(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM units WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no unit named {name!r}")
        return int(row["id"])


def _row_to_unit(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "path": row["path"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": json.loads(row["payload"]) if row["payload"] else {},
    }
