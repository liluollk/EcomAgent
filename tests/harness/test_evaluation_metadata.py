import harness.cases as cases
from harness.metrics import CaseRecord, summarize, summarize_real_rows
from harness.report import render_report


def test_all_scenarios_have_valid_metadata():
    cases.validate_scenarios(cases.SCENARIOS)
    assert len(cases.SCENARIOS) == 21
    assert {scenario["kind"] for scenario in cases.SCENARIOS} == {
        "gold",
        "guarded",
        "resilience",
        "recovery",
    }


def test_metadata_does_not_change_baseline_fingerprint():
    scenario = {"name": "x", "platform": "taobao", "steps": [{"message": "m"}]}
    tagged = {**scenario, "kind": "guarded", "metrics": ["permission_denied"],
              "real": True, "mode": "ASK", "fault": "verify_timeout"}

    assert cases.case_fingerprint(scenario) == cases.case_fingerprint(tagged)


def test_platform_and_expect_change_baseline_fingerprint():
    """平台与每步的执行契约期望都属于行为契约：改了就必须重新采样基线。

    否则「淘宝改成抖店」「副作用 1 条改成 2 条」这类语义变化会被指纹漏掉，
    两个平台的状态互相覆盖。
    """
    base = {"name": "x", "platform": "taobao",
            "steps": [{"message": "m", "expect": {"state": "SUCCEEDED"}}]}

    other_platform = {**base, "platform": "douyin"}
    other_state = {**base, "steps": [{"message": "m", "expect": {"state": "BLOCKED"}}]}

    assert cases.case_fingerprint(base) != cases.case_fingerprint(other_platform)
    assert cases.case_fingerprint(base) != cases.case_fingerprint(other_state)


def test_metrics_group_by_kind_and_domain_metric():
    records = [
        CaseRecord("a", "passed", 0.1, kind="guarded", metrics=["permission_denied"]),
        CaseRecord("b", "failed", 0.1, kind="resilience", metrics=["retry_contract"]),
        CaseRecord("c", "not_exercised", 0.1, kind="guarded", metrics=["permission_denied"]),
    ]

    metrics = summarize(records, 0.3)

    assert metrics.by_kind["guarded"] == {
        "cases": 2,
        "exercised": 1,
        "passed": 1,
        "failed": 0,
        "timeout": 0,
        "error": 0,
        "not_exercised": 1,
    }
    assert metrics.metric_hits["permission_denied"]["not_exercised"] == 1
    assert metrics.metric_hits["retry_contract"]["failed"] == 1


def test_report_contains_kind_and_metric_sections():
    records = [
        CaseRecord("a", "passed", 0.1, kind="guarded", metrics=["permission_denied"])
    ]

    text = render_report(summarize(records, 0.1), "run-1")

    assert "按类型:" in text
    assert "guarded" in text
    assert "领域指标:" in text
    assert "permission_denied" in text


def test_real_rows_mark_guarded_not_exercised_as_safe_no_op():
    summary = summarize_real_rows([
        {
            "name": "readonly_blocks_write",
            "kind": "guarded",
            "metrics": ["mode_gate_block"],
            "passes": 0,
            "triggers": 0,
            "not_exercised": 2,
        },
    ])

    assert summary["by_kind"]["guarded"]["safe_no_op"] == 1
    assert summary["metric_hits"]["mode_gate_block"]["not_exercised"] == 2
