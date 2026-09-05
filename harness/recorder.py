"""轨迹记录 — 每次运行一个 run_id 目录，AgentEvent 流逐条落盘。

目录结构：
    runs/{run_id}/
        cases.json                     参赛场景快照（输入定义 + 指纹）
        {case_name}/trace.jsonl        AgentEvent 流逐行 JSON（可回放）
        {case_name}/outcome.json       该 case 结论（状态/耗时/失败明细）
        report.txt / report.json       运行级汇总报告
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


def _serialize_event(ev: Any) -> dict:
    """AgentEvent dataclass → JSON 字典；未知类型兜底为原始字符串。"""
    if isinstance(ev, dict):
        return ev
    try:
        return asdict(ev)
    except Exception:
        return {"type": getattr(ev, "type", "?"), "raw": str(ev)}


class Recorder:
    """运行目录管理：创建 run_id、写场景快照、写 trace 与 outcome。"""

    def __init__(self, runs_root: Path) -> None:
        self.root = Path(runs_root)

    def new_run(self) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        (self.root / run_id).mkdir(parents=True, exist_ok=True)
        return run_id

    def save_cases(self, run_id: str, scenarios: Iterable[dict]) -> None:
        from harness.cases import case_fingerprint

        rows = [
            {"name": s["name"], "fingerprint": case_fingerprint(s), "steps": s["steps"]}
            for s in scenarios
        ]
        path = self.root / run_id / "cases.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def case_dir(self, run_id: str, case_name: str) -> Path:
        path = self.root / run_id / case_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_trace(self, run_id: str, case_name: str, events: Iterable[Any]) -> Path:
        lines = [json.dumps(_serialize_event(ev), ensure_ascii=False) for ev in events]
        path = self.case_dir(run_id, case_name) / "trace.jsonl"
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    def write_outcome(self, run_id: str, case_name: str, outcome: dict) -> Path:
        path = self.case_dir(run_id, case_name) / "outcome.json"
        path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_report(self, run_id: str, report_txt: str, report_json: dict) -> None:
        (self.root / run_id / "report.txt").write_text(report_txt, encoding="utf-8")
        (self.root / run_id / "report.json").write_text(
            json.dumps(report_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def latest_run_id(self) -> Optional[str]:
        runs = [d for d in self.root.iterdir() if d.is_dir() and d.name[:8].isdigit()]
        return max((d.name for d in runs), default=None) if runs else None