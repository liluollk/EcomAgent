"""验收 — 人类可读报告 + baseline 指纹对比（退化即非零退出码）。

baseline.json 结构：
    {
      "schema": 1,
      "saved_at": iso8601,
      "total": N,
      "passed": N,
      "cases": {case_name: fingerprint}   # 场景指纹（name+steps 规范哈希）
    }

对比规则：
    1. 当前评测集指纹集合 ≠ 基线指纹集合 → 不可比（评测集被改动），须
       重新 --save-baseline；
    2. 通过数 < 基线通过数 → 退化，失败退出；
    3. 其余（通过数持平或上升）→ 通过。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from harness.cases import case_fingerprint
from harness.metrics import Metrics


def render_report(metrics: Metrics, run_id: str) -> str:
    """人类可读报告（同时落盘 report.txt / report.json）。"""
    lines = [
        f"离线评测 Harness 报告  run_id={run_id}",
        "=" * 60,
        f"场景总数   : {metrics.total}",
        f"通过       : {metrics.passed}   ({metrics.pass_rate * 100:.1f}%)",
        f"断言失败   : {metrics.failed}",
        f"超时       : {metrics.timeout}",
        f"错误       : {metrics.errors}",
        f"总耗时     : {metrics.duration:.1f}s",
        "-" * 60,
        "case 明细:",
    ]
    for c in metrics.cases:
        flag = "PASS" if c.passed else c.status.upper()
        retry = f" (attempts={c.attempts})" if c.attempts > 1 else ""
        line = f"  [{flag:7s}] {c.name}{retry}"
        if not c.passed:
            for f in c.failures:
                line += f"\n             kind={f.get('kind')} detail={f.get('detail', '')}"
        lines.append(line)
    return "\n".join(lines)


def baseline_path(default: Path = Path("baseline.json")) -> Path:
    return default


def save_baseline(metrics: Metrics, path: Path) -> dict:
    """按当前指标与评测集指纹保存基线。"""
    blob = {
        "schema": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": metrics.total,
        "passed": metrics.passed,
        "cases": {s["name"]: case_fingerprint(s) for s in _current_scenarios(metrics)},
    }
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return blob


def load_baseline(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def compare_baseline(metrics: Metrics, baseline: dict) -> tuple[bool, str]:
    """对照基线验收。

    Returns:
        (ok, message): ok=False 表示退化或评测集不可比。
    """
    current = {s["name"]: case_fingerprint(s) for s in _current_scenarios(metrics)}
    baseline_cases = baseline.get("cases", {})
    if set(current) != set(baseline_cases) or any(
        current[n] != baseline_cases.get(n) for n in current
    ):
        changed = [n for n in current if current[n] != baseline_cases.get(n)]
        return False, (
            "评测集指纹与基线不一致（新增/删除/改动场景: "
            f"{', '.join(changed) or '集合变化'}），基线不可直接比对，请重新 --save-baseline"
        )
    if metrics.passed < baseline.get("passed", 0):
        return False, (
            f"行为退化：通过数 {metrics.passed} < 基线 {baseline.get('passed')}（基线保存于 "
            f"{baseline.get('saved_at', '?')}），请检查本次改动"
        )
    return True, (
        f"通过验收：{metrics.passed}/{metrics.total} 与基线 {baseline.get('passed')} 持平或更优"
    )


def _current_scenarios(metrics: Metrics) -> list[dict]:
    """从 metrics 反推参赛场景定义。

    说明：Metrics 未携带场景步骤全文（避免报告体积膨胀），改为从评测集
    按 case 名取回定义，指纹计算与运行完全一致。
    """
    from harness.cases import SCENARIOS

    names = {c.name for c in metrics.cases}
    return [s for s in SCENARIOS if s["name"] in names]