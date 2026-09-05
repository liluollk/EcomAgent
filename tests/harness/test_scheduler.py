"""调度器单测 — 顺序执行/进度回调/参数透传（run_case 用桩替换）。"""

from harness.metrics import CaseRecord
from harness.scheduler import run_all


class StubRunner:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def run_case(self, scenario, timeout=120.0, retries=0):
        self.calls.append((scenario["name"], timeout, retries))
        return self._outcomes.pop(0)


def _rec(name, status="passed"):
    return CaseRecord(name=name, status=status, duration=0.1)


def test_run_all_runs_every_scenario_in_order():
    runner = StubRunner([_rec("a"), _rec("b"), _rec("c")])
    scenarios = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    records = run_all(runner, scenarios, timeout=30.0, retries=1)
    assert [c.name for c in records] == ["a", "b", "c"]
    assert [c[0] for c in runner.calls] == ["a", "b", "c"]
    # timeout 与 retries 原样透传
    assert all(c[1] == 30.0 and c[2] == 1 for c in runner.calls)


def test_run_all_empty_scenarios():
    runner = StubRunner([])
    assert run_all(runner, []) == []


def test_run_all_progress_callback():
    runner = StubRunner([_rec("a"), _rec("b")])
    seen = []
    run_all(runner, [{"name": "a"}, {"name": "b"}], progress=lambda i, t, n: seen.append((i, t, n)))
    assert seen == [(1, 2, "a"), (2, 2, "b")]


def test_run_all_keeps_failed_and_timeout_records():
    runner = StubRunner([_rec("a", "failed"), _rec("b", "timeout"), _rec("c")])
    records = run_all(runner, [{"name": "a"}, {"name": "b"}, {"name": "c"}])
    assert [r.status for r in records] == ["failed", "timeout", "passed"]