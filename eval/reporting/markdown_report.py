"""Markdown 报告视图(计划 05 Task 7 Step 3)。

:func:`render_markdown` 把 :func:`eval.reporting.json_report.ReportBuilder.build`
产出的同一份 JSON dict 渲染为可读 Markdown——只是同一报告的视图,不新增、
不折算任何数字;报告没有综合总分,Markdown 同样没有。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_markdown(report: Mapping[str, Any]) -> str:
    """把 JSON 报告 dict 渲染为 Markdown(纯视图,不计算任何新数字)。"""
    lines: list[str] = [
        f"# Eval report: {report.get('report_type') or 'eval'}",
        "",
        f"- protocol_fingerprint: `{report.get('protocol_fingerprint') or ''}`",
        f"- benchmark_fingerprint: `{report.get('benchmark_fingerprint') or ''}`",
        f"- system_fingerprint: `{report.get('system_fingerprint') or ''}`",
        f"- capability_fingerprint: `{_manifest_fingerprint(report.get('capability_manifest'))}`",
    ]
    lines.extend(_config_lines(report.get("config")))
    totals = report.get("totals")
    if isinstance(totals, Mapping):
        lines.extend(_section("Totals"))
        lines.extend(f"- {key}: {_cell(value)}" for key, value in totals.items())
    metrics = report.get("metrics")
    if isinstance(metrics, Mapping):
        lines.extend(_section("结果"))
        lines.extend(_metric_lines(metrics))
        counts = metrics.get("counts")
        if isinstance(counts, Mapping):
            lines.extend(
                f"- {key}: {_cell(counts.get(key))}"
                for key in (
                    "actual_dangerous_side_effects",
                    "dangerous_attempts",
                    "prevented_attempts",
                )
            )
        stability = metrics.get("scenario_stability")
        if isinstance(stability, Mapping):
            lines.extend(
                [
                    "- stable_scenarios: {passed}/{total} ({rate})".format(
                        passed=_cell(stability.get("stable_passed_scenarios")),
                        total=_cell(stability.get("total_scenarios")),
                        rate=_rate_text(stability.get("stable_pass_rate")),
                    )
                ]
            )
        diagnostics = metrics.get("diagnostics")
        if isinstance(diagnostics, Mapping):
            lines.extend(_section("失败诊断"))
            lines.extend(_metric_lines(diagnostics))
    cases = report.get("cases")
    if isinstance(cases, list) and cases:
        lines.extend(_section("Cases"))
        lines.append("| case_id | repetition | status | included | error |")
        lines.append("| --- | --- | --- | --- | --- |")
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            lines.append(
                "| {case_id} | {repetition} | {status} | {included} | {error} |".format(
                    case_id=_cell(case.get("case_id")),
                    repetition=_cell(case.get("repetition")),
                    status=_cell(case.get("status")),
                    included=_cell(case.get("included_in_quality_score")),
                    error=_cell(case.get("error")),
                )
            )
    errors = report.get("infrastructure_errors")
    if isinstance(errors, list) and errors:
        lines.extend(_section("Infrastructure Errors"))
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(
                    f"- case={_cell(error.get('case_id'))} "
                    f"rep={_cell(error.get('repetition'))}: {_cell(error.get('error'))}"
                )
    return "\n".join(lines) + "\n"


def _config_lines(config: Any) -> list[str]:
    """配置摘要行(config 缺失时为空;数值全部来自报告 JSON)。"""
    if not isinstance(config, Mapping):
        return []
    return [
        "- model: {model} (temperature={temperature}, repetitions={repetitions}, "
        "scorer_version={scorer_version})".format(
            model=_cell(config.get("model")),
            temperature=_cell(config.get("temperature")),
            repetitions=_cell(config.get("repetitions")),
            scorer_version=_cell(config.get("scorer_version")),
        )
    ]


def _manifest_fingerprint(manifest: Any) -> str:
    if not isinstance(manifest, Mapping):
        return ""
    value = manifest.get("capability_fingerprint")
    return value if isinstance(value, str) else ""


def _metric_lines(metrics: Mapping[str, Any]) -> list[str]:
    """分项比率行:值 + 括号内真实分子/分母(缺省子计数时只给值)。"""
    counts = metrics.get("counts")
    denominators = metrics.get("denominators")
    counts = counts if isinstance(counts, Mapping) else {}
    denominators = denominators if isinstance(denominators, Mapping) else {}
    quality = denominators.get("included_in_quality_score")
    detail_by_metric = {
        "task_completion_rate": (counts.get("task_passed"), quality),
    }
    lines: list[str] = []
    for key, value in metrics.items():
        if key in ("counts", "denominators", "diagnostics", "scenario_stability"):
            continue
        detail = ""
        if key in detail_by_metric:
            numerator, denominator = detail_by_metric[key]
            if numerator is not None and denominator is not None:
                detail = f" ({numerator}/{denominator})"
        lines.append(f"- {key}: {_rate_text(value)}{detail}")
    return lines


def _rate_text(value: Any) -> str:
    """比率格式化:数字四位小数,``None`` 显式 ``n/a``(不伪装成 0)。"""
    if value is None:
        return "n/a"
    if isinstance(value, int | float):
        return f"{value:.4f}"
    return _cell(value)


def _section(title: str) -> list[str]:
    return ["", f"## {title}", ""]


def _cell(value: Any) -> str:
    """Markdown 单元格文本:``None`` → ``n/a``,其余 str()。"""
    if value is None:
        return "n/a"
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


__all__ = ["render_markdown"]
