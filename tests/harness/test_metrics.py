"""指标汇总单测 — 分类计数与通过率。"""

from harness.metrics import CaseRecord, summarize


def _rec(name, status):
    return CaseRecord(name=name, status=status, duration=0.5)


def test_summarize_classifies_statuses():
    records = [
        _rec("a", "passed"),
        _rec("b", "passed"),
        _rec("c", "failed"),
        _rec("d", "timeout"),
        _rec("e", "error"),
    ]
    m = summarize(records, total_duration=3.0)
    assert m.total == 5
    assert m.passed == 2
    assert m.failed == 1
    assert m.timeout == 1
    assert m.errors == 1
    assert m.duration == 3.0


def test_pass_rate():
    m = summarize([_rec("a", "passed"), _rec("b", "failed")], total_duration=1.0)
    assert m.pass_rate == 0.5
    assert summarize([], total_duration=0.0).pass_rate == 0.0


def test_summary_dict_shape():
    m = summarize([_rec("a", "passed")], total_duration=1.0)
    d = m.summary_dict()
    assert d["total"] == 1 and d["passed"] == 1
    assert d["cases"]["a"]["status"] == "passed"
    assert "pass_rate" in d and "duration_s" in d