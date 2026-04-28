"""Tests for cabinet.worklist."""

from __future__ import annotations

from pathlib import Path

import pytest

from cabinet.worklist import STATUSES, UNIT_KINDS, Worklist


def test_worklist_creates_database(tmp_path: Path):
    db = tmp_path / "wl.db"
    with Worklist(db):
        pass
    assert db.exists()


def test_add_unit_and_lookup(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/foo/bar", metadata={"file_count": 12})

        unit = wl.unit(uid)
        assert unit is not None
        assert unit.kind == "folder"
        assert unit.path == "/foo/bar"
        assert unit.status == "discovered"
        assert unit.metadata["file_count"] == 12


def test_add_unit_is_idempotent_upsert(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        a = wl.add_unit("folder", "/foo")
        b = wl.add_unit("folder", "/foo", metadata={"updated": True})
        assert a == b

        unit = wl.unit(a)
        assert unit is not None
        assert unit.metadata == {"updated": True}


def test_status_lifecycle(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x")
        for status in ("scanned", "classified", "triaged", "approved", "applied"):
            wl.update_status(uid, status)
            assert wl.unit(uid).status == status


def test_invalid_status_rejected(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x")
        with pytest.raises(ValueError):
            wl.update_status(uid, "explode")


def test_invalid_kind_rejected(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        with pytest.raises(ValueError):
            wl.add_unit("not-a-kind", "/x")


def test_record_finding_and_lookup(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x")
        fid = wl.record_finding(
            uid,
            rule="by_extension",
            confidence=0.9,
            classifications=["photos"],
            evidence={"share": 1.0},
            source_paths=["/x/IMG_0001.jpg"],
        )
        assert fid > 0
        findings = wl.findings_for(uid)
        assert len(findings) == 1
        f = findings[0]
        assert f.rule == "by_extension"
        assert f.confidence == 0.9
        assert f.classifications == ["photos"]
        assert f.evidence == {"share": 1.0}
        assert f.source_paths == ["/x/IMG_0001.jpg"]


def test_invalid_confidence_rejected(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x")
        with pytest.raises(ValueError):
            wl.record_finding(uid, rule="r", confidence=2.5)


def test_record_decision(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x")
        wl.record_decision(uid, "keep", payload={"note": "looks fine"})
        wl.record_decision(uid, "archive")
        decisions = wl.decisions_for(uid)
        assert [d.action for d in decisions] == ["keep", "archive"]
        assert decisions[0].payload == {"note": "looks fine"}


def test_units_by_status(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        a = wl.add_unit("folder", "/a")
        b = wl.add_unit("folder", "/b")
        wl.update_status(b, "scanned")

        scanned = wl.units_by_status("scanned")
        discovered = wl.units_by_status("discovered")

        assert [u.path for u in scanned] == ["/b"]
        assert [u.path for u in discovered] == ["/a"]


def test_run_lifecycle(tmp_path: Path):
    with Worklist(tmp_path / "wl.db") as wl:
        rid = wl.start_run("scan")
        wl.event("walking", run_id=rid, payload={"dir": "/x"})
        wl.end_run(rid, summary={"folders": 3})

        runs = wl.runs()
        assert len(runs) == 1
        assert runs[0].phase == "scan"
        assert runs[0].ended_at is not None
        assert runs[0].summary == {"folders": 3}

        events = wl.events_for_run(rid)
        assert len(events) == 1
        assert events[0]["kind"] == "walking"
        assert events[0]["payload"] == {"dir": "/x"}


def test_no_sql_injection_via_path(tmp_path: Path):
    """Pathological paths must not corrupt the schema or leak data."""
    nasty = "/tmp/'; DROP TABLE units; --"
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", nasty)
        unit = wl.unit(uid)
        assert unit is not None
        assert unit.path == nasty

        # Schema still intact — we can still query.
        assert len(wl.all_units()) == 1


def test_no_sql_injection_via_metadata(tmp_path: Path):
    payload = {"note": "'; DROP TABLE units; --"}
    with Worklist(tmp_path / "wl.db") as wl:
        uid = wl.add_unit("folder", "/x", metadata=payload)
        unit = wl.unit(uid)
        assert unit.metadata == payload


def test_persistence_across_reopen(tmp_path: Path):
    db = tmp_path / "wl.db"
    with Worklist(db) as wl:
        uid = wl.add_unit("folder", "/persist")
        wl.update_status(uid, "scanned")

    with Worklist(db) as wl2:
        unit = wl2.unit_by_path("folder", "/persist")
        assert unit is not None
        assert unit.status == "scanned"


def test_constants_exposed():
    assert "discovered" in STATUSES
    assert "applied" in STATUSES
    assert "folder" in UNIT_KINDS
    assert "duplicate-pair" in UNIT_KINDS
