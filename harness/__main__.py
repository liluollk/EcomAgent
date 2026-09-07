"""harness CLI — python -m harness [选项]

示例：
    python -m harness                     # 回归验收：剧本后端跑全部场景，对照基线
    python -m harness --save-baseline     # 跑全部并保存/更新基线
    python -m harness --case six_step_business_chain --case cost_interception
    python -m harness --timeout 180 --retries 1 --runs-dir .harness-runs
    python -m harness --backend openai --provider deepseek --repeat 3
                                          # 真实模型评测：运行时契约场景 ×3，独立报告
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from harness.cases import SCENARIOS, real_model_scenarios
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
    p.add_argument("--backend", choices=["mock", "openai", "anthropic"], default="mock",
                   help="决策源后端：mock=剧本回归验收（缺省）；openai/anthropic=真实模型评测")
    p.add_argument("--provider", default=None, metavar="NAME",
                   help="真实模型模式使用 providers.json 中的指定供应商（如 deepseek）")
    p.add_argument("--repeat", type=int, default=1,
                   help="真实模型模式每场景重复次数（吸收 run 间方差，默认 1）")
    p.add_argument("--real-only", action="store_true",
                   help="只跑运行时契约场景（真实模型模式自动启用）")
    return p


def _run_real_mode(args, runner: Runner, scenarios: list, recorder: Recorder, run_id: str) -> int:
    """真实模型评测模式：逐场景重复 N 次统计，独立报告，不读写回归基线。

    三态判定（契约触发口径）：
      PASS           契约被触发的轮次全部通过（触发即正确 = 稳定）
      NOT-EXERCISED  模型从未触发期望业务工具——非运行时失败，不计入失败
      FLAKY/FAIL     契约触发过但存在断言/超时失败

    通过率 = 触发轮次中通过的占比（分母剔除 not-exercised）。
    """
    n = max(1, args.repeat)
    rows: list[dict] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    print(f"真实模型评测  backend={args.backend}  provider={args.provider or '(激活供应商)'}  repeat={n}")
    stable = not_ex = flaky = 0
    for idx, scenario in enumerate(scenarios, start=1):
        passes = 0
        not_exercised = 0
        durations: list[float] = []
        last_fail = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for _ in range(n):
            rec = runner.run_case(scenario, timeout=args.timeout, retries=args.retries)
            if rec.passed:
                passes += 1
            elif rec.status == "not_exercised":
                not_exercised += 1
            durations.append(rec.duration)
            if rec.failures:
                last_fail = str(rec.failures[-1].get("detail", ""))[:120]
            u = runner.last_usage or {}
            usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage["completion_tokens"] += u.get("completion_tokens", 0)
        triggers = n - not_exercised
        if triggers == 0:
            verdict = "NOT-EXERCISED"
            not_ex += 1
        elif passes == triggers:
            verdict = "PASS"
            stable += 1
        else:
            verdict = "FLAKY/FAIL"
            flaky += 1
        mean_dur = sum(durations) / len(durations)
        for k in total_usage:
            total_usage[k] += usage[k]
        rows.append({"name": scenario["name"], "passes": passes, "not_exercised": not_exercised,
                     "triggers": triggers, "verdict": verdict, "mean_duration_s": round(mean_dur, 2),
                     "usage": usage, "last_fail": last_fail})
        mark = verdict if verdict == "PASS" else ("NOT-EX" if verdict == "NOT-EXERCISED" else "FAIL")
        print(f"  [{idx}/{len(scenarios)}] {scenario['name']:34s} {passes}/{n} "
              f"{mark:9s} avg {mean_dur:.1f}s  "
              f"tok {usage['prompt_tokens']}+{usage['completion_tokens']}"
              + (f"  <- {last_fail}" if verdict != "PASS" and last_fail else ""))

    triggered_runs = sum(r["triggers"] for r in rows)
    passed_runs = sum(r["passes"] for r in rows)
    trigger_rate = round(passed_runs / triggered_runs, 4) if triggered_runs else 0.0
    report = {"mode": "real_model", "backend": args.backend, "provider": args.provider,
              "repeat": n, "scenarios": rows, "total_usage": total_usage,
              "stable": stable, "not_exercised": not_ex, "flaky": flaky,
              "trigger_pass_rate": trigger_rate}
    recorder.write_report(run_id, "\n".join(
        f"{r['name']}: {r['passes']}/{n} [{r['verdict']}]" for r in rows), report)
    print(f"\n真实模型评测完成：稳定通过 {stable}/{len(scenarios)}，"
          f"not-exercised {not_ex}，FLAKY/FAIL {flaky}"
          f"   契约触发通过率 {trigger_rate:.1%}"
          f"   总 token {total_usage['prompt_tokens']}+{total_usage['completion_tokens']}")
    if flaky:
        print("存在契约触发后的失败场景（见上方 FAIL 行）——运行时有真实缺口或断言过严，需人工复核。")
    else:
        print("无契约触发后的失败：not-exercised 场景为模型未触发（不构成运行时缺陷）。")
    print("（真实模型模式不读写回归基线——方差下二值基线语义不适用）")
    return 0 if flaky == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list:
        for s in SCENARIOS:
            print(f"  {s['name']:32s} steps={len(s['steps'])}{'  [real]' if s.get('real') else ''}")
        return 0

    real_mode = args.backend != "mock"
    pool = real_model_scenarios() if (real_mode or args.real_only) else SCENARIOS
    scenarios = [s for s in pool if s["name"] in args.case] if args.case else list(pool)

    recorder = Recorder(Path(args.runs_dir))
    run_id = recorder.new_run()
    recorder.save_cases(run_id, scenarios)

    runner = Runner(recorder, run_id, backend=args.backend, provider=args.provider)

    if real_mode:
        return _run_real_mode(args, runner, scenarios, recorder, run_id)

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