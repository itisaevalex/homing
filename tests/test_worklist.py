"""Tests for ``homing.worklist`` against an in-memory SQLite DB."""

from __future__ import annotations

import pytest

from homing.worklist import VALID_STATUSES, Worklist


@pytest.fixture
def wl() -> Worklist:
    """Fresh in-memory worklist per test."""
    w = Worklist(":memory:")
    yield w
    w.close()


def test_add_unit_starts_in_discovered(wl: Worklist) -> None:
    wl.add_unit("project", "alpha", "/home/u/alpha", payload={"signals": [".git"]})
    unit = wl.unit("alpha")
    assert unit is not None
    assert unit["kind"] == "project"
    assert unit["status"] == "discovered"
    assert unit["payload"] == {"signals": [".git"]}


def test_unit_returns_none_for_unknown(wl: Worklist) -> None:
    assert wl.unit("nope") is None


def test_add_unit_rejects_bad_kind(wl: Worklist) -> None:
    with pytest.raises(ValueError):
        wl.add_unit("widget", "x", "/x")


def test_unit_name_uniqueness(wl: Worklist) -> None:
    wl.add_unit("project", "dup", "/a")
    with pytest.raises(Exception):
        wl.add_unit("project", "dup", "/b")


def test_update_status_transitions(wl: Worklist) -> None:
    wl.add_unit("project", "alpha", "/a")
    for s in VALID_STATUSES:
        wl.update_status("alpha", s)
        assert wl.unit("alpha")["status"] == s


def test_update_status_rejects_unknown(wl: Worklist) -> None:
    wl.add_unit("project", "alpha", "/a")
    with pytest.raises(ValueError):
        wl.update_status("alpha", "imaginary")


def test_update_status_unknown_unit(wl: Worklist) -> None:
    with pytest.raises(KeyError):
        wl.update_status("nobody", "discovered")


def test_units_by_status_filters(wl: Worklist) -> None:
    wl.add_unit("project", "a", "/a")
    wl.add_unit("project", "b", "/b")
    wl.add_unit("place", "c", "/c")
    wl.update_status("a", "rules-evaluated")
    discovered = wl.units_by_status("discovered")
    assert sorted(u["name"] for u in discovered) == ["b", "c"]
    rules_evaluated = wl.units_by_status("rules-evaluated")
    assert [u["name"] for u in rules_evaluated] == ["a"]


def test_record_finding_round_trip(wl: Worklist) -> None:
    wl.add_unit("project", "alpha", "/a")
    fid = wl.record_finding(
        "alpha",
        rule="is-git-project",
        confidence=1.0,
        classifications={"is_git_project": True},
        evidence=[("/a/.git", ".git directory present at project root")],
    )
    assert fid > 0
    findings = wl.findings_for("alpha")
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "is-git-project"
    assert f["confidence"] == 1.0
    assert f["classifications"] == {"is_git_project": True}
    assert f["evidence"] == [["/a/.git", ".git directory present at project root"]]


def test_record_finding_rejects_unknown_unit(wl: Worklist) -> None:
    with pytest.raises(KeyError):
        wl.record_finding("ghost", "r", 1.0, {}, [])


def test_run_lifecycle(wl: Worklist) -> None:
    rid = wl.start_run("homing enumerate")
    assert rid > 0
    wl.end_run(rid, exit_code=0, summary="ok")
    # ending an unknown run id raises
    with pytest.raises(KeyError):
        wl.end_run(99999, 0, "")


def test_event_attached_to_unit(wl: Worklist) -> None:
    wl.add_unit("project", "alpha", "/a")
    wl.event("alpha", type="info", message="walked")
    events = wl.events_for("alpha")
    assert len(events) == 1
    assert events[0]["type"] == "info"
    assert events[0]["message"] == "walked"


def test_event_with_no_unit_is_global(wl: Worklist) -> None:
    eid = wl.event(None, type="info", message="run started")
    assert eid > 0


def test_no_sql_injection_via_unit_name(wl: Worklist) -> None:
    """Names with SQL metacharacters must round-trip safely.

    If ``unit()`` interpolated the name into a query, this would either
    crash or return the wrong row. Parameterised queries make the name
    a literal blob.
    """
    nasty = "alpha'; DROP TABLE units; --"
    wl.add_unit("project", nasty, "/a")
    # Tables still exist?
    assert wl.unit(nasty) is not None
    assert wl.unit(nasty)["name"] == nasty
    # Add another unit; the units table must still be there.
    wl.add_unit("project", "beta", "/b")
    assert wl.unit("beta") is not None


def test_no_sql_injection_via_status(wl: Worklist) -> None:
    """Even if a malicious status string slipped past the enum check, it
    would be a parameter, not concatenated SQL. The enum check rejects
    it first; the parameterisation is the second line of defence.
    """
    wl.add_unit("project", "alpha", "/a")
    with pytest.raises(ValueError):
        wl.update_status("alpha", "discovered'; DROP TABLE units; --")
    # And the original unit is intact.
    assert wl.unit("alpha")["status"] == "discovered"


def test_idempotent_init_creates_tables_once(tmp_path) -> None:
    """Constructing a Worklist twice on the same file must not error."""
    db_path = tmp_path / "wl.sqlite"
    a = Worklist(db_path)
    a.add_unit("project", "alpha", "/a")
    a.close()
    # Re-open: tables already exist, must not raise, must see prior data.
    b = Worklist(db_path)
    assert b.unit("alpha") is not None
    b.close()


def test_full_workflow(wl: Worklist) -> None:
    """Discovery → rules → classified → drafted → validated → resolved."""
    wl.add_unit("project", "alpha", "/home/u/alpha", payload={"signals": [".git"]})
    rid = wl.start_run("homing rules")
    wl.record_finding("alpha", "is-git-project", 1.0, {"is_git_project": True}, [])
    wl.update_status("alpha", "rules-evaluated")
    wl.event("alpha", "info", "1 rule fired")
    wl.update_status("alpha", "classified")
    wl.update_status("alpha", "drafted")
    wl.update_status("alpha", "validated")
    wl.update_status("alpha", "resolved")
    wl.end_run(rid, 0, "1 unit processed")

    assert wl.unit("alpha")["status"] == "resolved"
    assert len(wl.findings_for("alpha")) == 1
    assert len(wl.events_for("alpha")) == 1
