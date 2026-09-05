"""调度 — 顺序执行评测集，case 级超时与失败重试由 runner 承担。

并发说明：MCP 的 stdio 子进程绑定创建时的事件循环，case 并发需每 case
独立进程/线程。评测优先确定性（顺序执行 + 每次运行独立 env），当前不做
并发调度；多 case 并行留给后续 if needed。
"""

from __future__ import annotations

from typing import Callable, Optional

from harness.metrics import CaseRecord
from harness.runner import Runner


def run_all(
    runner: Runner,
    scenarios: list[dict],
    timeout: float = 120.0,
    retries: int = 0,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[CaseRecord]:
    """顺序执行全部场景。

    Args:
        runner: 评测运行器。
        scenarios: 评测集（harness.cases.SCENARIOS 或其子集）。
        timeout: 单个 case 的超时秒数。
        retries: 失败/超时后的重试次数。
        progress: 开始前回调 (index, total, case_name)。
    """
    records: list[CaseRecord] = []
    total = len(scenarios)
    for idx, scenario in enumerate(scenarios, start=1):
        if progress:
            progress(idx, total, scenario["name"])
        records.append(runner.run_case(scenario, timeout=timeout, retries=retries))
    return records