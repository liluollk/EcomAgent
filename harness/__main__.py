"""harness CLI — python -m harness [选项]

示例：
    python -m harness                     # 跑全部场景，对照已有基线（无基线则提示保存）
    python -m harness --save-baseline     # 跑全部并保存/更新基线
    python -m harness --case six_step_business_chain --case cost_interception
    python -m harness --timeout 180 --retries 1 --runs-dir .harness-runs
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from harness.cases import SCENARIOS
from harness.metrics import summarize
from harness.recorder import Recorder
from harness.report import compare_baseline, load_baseline, render_report, save_baseline
from harness.runner import Runner
from harness.scheduler import run_all


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m harness",
        description="离线评测 Harness — 行为契约评测基座（准备任务/驱动运行/记录轨迹/验收对比）",
    )
    p.add_argument("--case", action="append", default=[], metavar="NAME",
                   help="只跑指定场景（可多次指定）；缺省跑全部")
    p.add_argument("--timeout", type=float, default=120.0, help="单 case 超时秒数（默认 120）")
    p.add_argument("--retries", type=int, default=0, help="失败/超时重试次数（默认 0）")
    p.add_argument("--runs-dir", default=".harness-runs", help="运行产物根目录（默认 .harness-runs）")
    p.add_argument("--baseline", default="baseline.json", help="基线文件路径（默认 baseline.json）")
    p.add_argument("--save-baseline", action="store_true", help="运行通过后保存/更新基线")
    p.add_argument("--list", action="store_true", help="列出评测集场景，不运行")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list:
        for s in SCENARIOS:
            print(f"  {s['name']:32s} steps={len(s['steps'])}")
        return 0

    scenarios = [s for s in SCENARIOS if s["name"] in args.case] if args.case else list(SCENARIOS)

    recorder = Recorder(Path(args.runs_dir))
    run_id = recorder.new_run()
    recorder.save_cases(run_id, scenarios)

    runner = Runner(recorder, run_id)

    def _progress(idx: int, total: int, name: str) -> None:
        print(f"  [{idx}/{total}] {name} ...", flush=True)

    start = time.monotonic()
    records = run_all(runner, scenarios, timeout=args.timeout, retries=args.retries,
                      progress=_progress)
    metrics = summarize(records, time.monotonic() - start)

    report_txt = render_report(metrics, run_id)
    print()
    print(report_txt)
    recorder.write_report(run_id, report_txt, metrics.summary_dict())

    baseline_file = Path(args.baseline)
    if args.save_baseline:
        blob = save_baseline(metrics, baseline_file)
        print(f"\n基线已保存: {baseline_file} (passed={blob['passed']}/{blob['total']}, "
              f"cases={len(blob['cases'])})")
        return 0

    baseline = load_baseline(baseline_file)
    if baseline is None:
        print(f"\n未发现基线文件 {baseline_file}；运行已通过。"
              "如需建立回归基线，请加 --save-baseline 再跑一次。")
        return 0

    ok, message = compare_baseline(metrics, baseline)
    print(f"\n基线验收: {'PASS' if ok else 'FAIL'} — {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())