"""harness 自身单测 — 评测基座逻辑（含基线对比与验收决策）。"""

from harness.cases import SCENARIOS, case_fingerprint


def test_fingerprint_stable_across_calls():
    fp1 = case_fingerprint(SCENARIOS[0])
    fp2 = case_fingerprint(SCENARIOS[0])
    assert fp1 == fp2
    assert len(fp1) == 16


def test_fingerprint_changes_when_steps_change():
    orig = case_fingerprint(SCENARIOS[0])
    mutated = dict(SCENARIOS[0], steps=[{"message": "改了一句", "expect_tool": False}])
    assert case_fingerprint(mutated) != orig


def test_fingerprint_ignores_setup_after_fields():
    """指纹只含 name+steps：钩子字段变化不影响契约可比性。"""
    base = SCENARIOS[2]  # dynamic_channel_add（带 setup）
    mutated = dict(base, setup="")
    assert case_fingerprint(mutated) == case_fingerprint(base)