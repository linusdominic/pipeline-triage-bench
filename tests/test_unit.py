"""Fast tests that need no generated world."""

from __future__ import annotations

from datetime import date, datetime

from triagebench.scoring import aggregate, match_rule
from triagebench.tasks import catalog
from triagebench.tasks.spec import DECISIONS, root_cause_vocabulary
from triagebench.world import calendar as cal
from triagebench.world import datagen
from triagebench.world.actions import set_monitor_params


def test_calendar() -> None:
    assert not cal.is_business_day(date(2026, 11, 11))  # Veterans Day
    assert not cal.is_business_day(date(2026, 11, 14))  # Saturday
    assert cal.prev_business_day(date(2026, 11, 12)) == date(2026, 11, 10)
    assert cal.prev_weekday(date(2026, 11, 12)) == date(2026, 11, 11)
    assert cal.bureau_delivery_day(date(2026, 10, 14)) == date(2026, 10, 13)  # Monday 10-12 is a holiday
    assert cal.run_time_for(date(2026, 11, 13)) == datetime(2026, 11, 14, 6, 0)


def test_datagen_is_deterministic() -> None:
    a, b = datagen.generate(7), datagen.generate(7)
    assert a.files.keys() == b.files.keys()
    for k in a.files:
        assert a.files[k].render() == b.files[k].render()
        assert a.files[k].arrived_at == b.files[k].arrived_at
    c = datagen.generate(8)
    assert any(a.files[k].render() != c.files[k].render() for k in a.files if k in c.files)


def test_trailers_match_rows() -> None:
    tl = datagen.generate(7)
    f = tl.files[("payments", date(2026, 11, 5))]
    assert f.trailer is not None and f.trailer[0] == len(f.rows)
    assert f.trailer == datagen.trailer_for(f.rows, 3)


def test_catalog_shape() -> None:
    tasks = catalog.all_tasks()
    assert len(tasks) == 33
    assert {t.family for t in tasks} == set(catalog.FAMILIES)
    assert all(t.decision in DECISIONS for t in tasks)
    vocab = set(root_cause_vocabulary())
    assert all(t.root_cause in vocab for t in tasks)
    no_action = [t for t in tasks if t.decision == "no_action"]
    assert len(no_action) == 9
    assert sum(1 for t in tasks if t.noise) >= 5


def test_set_monitor_params_keeps_comments() -> None:
    text = (
        "monitors:\n"
        "  - name: a\n"
        "    type: volume\n"
        "    threshold: 0.35                # alert above this\n"
        "\n"
        "  - name: b\n"
        "    type: freshness\n"
    )
    out = set_monitor_params(text, "a", {"threshold": 0.4, "baseline_start": "2026-11-02"})
    assert "    threshold: 0.4                # alert above this\n" in out
    assert "    baseline_start: '2026-11-02'\n" in out
    assert out.index("baseline_start") < out.index("- name: b")
    out = set_monitor_params(text, "b", {"calendar": "business_calendar"})
    assert out.rstrip().endswith("calendar: business_calendar")


def test_match_rule() -> None:
    call = {"tool": "backfill", "args": {"target": "all", "partitions": ["2026-11-05"]}}
    assert match_rule({"tool": "backfill", "args": {"target": ["all"], "partitions": ["2026-11-05"]}}, call, {})
    assert not match_rule({"tool": "backfill", "args": {"partitions": ["2026-11-04"]}}, call, {})
    upd = {"tool": "update_monitor", "args": {"name": "v", "params": {"threshold": 0.3}}}
    assert match_rule({"tool": "update_monitor", "args": {"name": ["v"], "params": {"threshold": [0.2, 0.6]}}}, upd, {})
    assert not match_rule(
        {"tool": "update_monitor", "args": {"name": ["v"], "params": {"threshold": [0.4, 0.6]}}}, upd, {}
    )
    rev = {"tool": "revert_commit", "args": {"sha": "abcdef12"}, "result_reverted": "abcdef1234567"}
    assert match_rule({"tool": "revert_commit", "args": {"sha": ["{bad_sha}"]}}, rev, {"bad_sha": "abcdef1234567"})
    assert not match_rule({"tool": "revert_commit", "args": {"sha": ["{bad_sha}"]}}, rev, {"bad_sha": "0000000"})


def test_aggregate_rates() -> None:
    rows = [
        {
            "family": "f",
            "expected_decision": "no_action",
            "resolved": True,
            "decision_correct": True,
            "root_cause_correct": True,
            "outcome_correct": True,
            "unnecessary_actions": 0,
            "forbidden_actions": 0,
            "restraint": 1.0,
            "tool_calls": 4,
            "input_tokens": None,
            "output_tokens": None,
        },
        {
            "family": "g",
            "expected_decision": "rerun",
            "resolved": False,
            "decision_correct": False,
            "root_cause_correct": True,
            "outcome_correct": False,
            "unnecessary_actions": 2,
            "forbidden_actions": 1,
            "restraint": 0.0,
            "tool_calls": 6,
            "input_tokens": None,
            "output_tokens": None,
        },
    ]
    o = aggregate(rows)["overall"]
    assert o["resolved_rate"] == 0.5
    assert o["false_alarm_restraint_rate"] == 1.0
    assert o["unnecessary_action_rate"] == 0.5
    assert o["destructive_action_rate"] == 0.5
    assert o["mean_tool_calls"] == 5.0
