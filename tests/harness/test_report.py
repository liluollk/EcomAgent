"""验收报告单测 — 基线往返、三条对比分支（持平/退化/指纹变化）、报告渲染。"""

from pathlib import Path

from harness.cases import SCENARIOS, case_fingerprint
from harness.metrics import CaseRecord, Metrics, summarize
from harness.report import compare_baseline, load_baseline, render_report, save_baseline

_NAMES = [s["name"] for s in SCENARIOS]


def _metrics(names, passed_names=None):
    """按给定 case 名集合构 metrics；passed_names 控制哪些记为通过。"""
    if passed_names is None:
        passed_names = names
    records = [
        CaseRecord(name=n, status="passed" if n in passed_names else "failed", duration=0.5)
        for n in names
    ]
    return summarize(records, total_duration=2.0)


def _baseline(passed=4):
    return {
        "schema": 1,
        "saved_at": "2026-09-05T00:00:00+00:00",
        "total": 4,
        "passed": passed,
        "cases": {s["name"]: case_fingerprint(s) for s in SCENARIOS},
    }


def test_baseline_roundtrip(tmp_path):
    m = _metrics(_NAMES)
    path = Path(tmp_path) / "baseline.json"
    blob = save_baseline(m, path)
    loaded = load_baseline(path)
    assert loaded == blob
    assert loaded["passed"] == len(_NAMES)
    assert set(loaded["cases"]) == set(_NAMES)


def test_compare_pass_at_parity():
    ok, msg = compare_baseline(_metrics(_NAMES), _baseline(passed=4))
    assert ok
    assert "持平或更优" in msg


def test_compare_regression_detected():
    ok, msg = compare_baseline(_metrics(_NAMES, _NAMES[:3]), _baseline(passed=4))
    assert not ok
    assert "退化" in msg
    assert "3 < 基线 4" in msg


def test_compare_fingerprint_change_detected():
    baseline = _baseline()
    baseline["cases"]["six_step_business_chain"] = "deadbeef" * 4  # 篡改指纹
    ok, msg = compare_baseline(_metrics(_NAMES), baseline)
    assert not ok
    assert "指纹" in msg
    assert "six_step_business_chain" in msg


def test_compare_partial_run_vs_full_baseline():
    """只跑子集时与全量基线不可比（集合变化）。"""
    m = _metrics(_NAMES[:1])
    ok, msg = compare_baseline(m, _baseline(passed=4))
    assert not ok
    assert "集合变化" in msg or "指纹" in msg


def test_render_report_contains_metrics_and_details():
    m = _metrics(_NAMES, _NAMES[:3])  # 前 3 过其余败：PASS 与 FAIL 行都出现
    text = render_report(m, run_id="20260905-000000-000001")
    assert "场景总数" in text and str(len(_NAMES)) in text
    assert "PASS" in text and "FAIL" in text
    assert "run_id=20260905-000000-000001" in text


def test_load_baseline_missing_file(tmp_path):
    assert load_baseline(Path(tmp_path) / "nope.json") is None