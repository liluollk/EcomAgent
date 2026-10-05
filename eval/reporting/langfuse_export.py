"""Langfuse 报告导出(计划 05 Task 7 Step 3)。

:func:`export_report` 把 JSON 报告作为**单个** ingest 项提交给注入的
客户端(复用 :class:`adapters.langfuse.client.LangfuseClient` 的
``ingest`` 批提交契约);导出前整份报告再经
:class:`runtime.observability.redaction.TraceRedactor` 双层脱敏——报告
本体虽已脱敏,导出层仍按"方向宁多勿漏"再过一遍,Secret 绝不入 Langfuse。

客户端经参数注入(测试注入内存 fake,不发真实网络);eval 不 import apps,
``adapters`` 属可依赖面。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol, runtime_checkable

from runtime.observability.redaction import TraceRedactor


_REPORT_ITEM_TYPE: Final = "eval-report-create"
_REPORT_NAME: Final = "eval.product_suite"


@runtime_checkable
class ReportExportClient(Protocol):
    """报告导出客户端的最小能力面(与 LangfuseClient.ingest 同形)。"""

    async def ingest(self, batch: Sequence[Mapping[str, Any]]) -> None:
        """原子提交一批 ingest 项;失败由客户端实现归一抛错。"""
        ...


async def export_report(
    report: Mapping[str, Any],
    client: ReportExportClient,
    *,
    redactor: TraceRedactor | None = None,
) -> None:
    """脱敏后把报告作为单个 ingest 项导出;客户端注入,本函数不发网络。"""
    active = redactor if redactor is not None else TraceRedactor()
    redacted: dict[str, Any] = active.redact(dict(report))
    fingerprint = str(report.get("protocol_fingerprint") or _REPORT_NAME)
    await client.ingest(
        [
            {
                "type": _REPORT_ITEM_TYPE,
                "id": fingerprint,
                "body": {"name": _REPORT_NAME, "input": redacted},
            }
        ]
    )


__all__ = ["ReportExportClient", "export_report"]
