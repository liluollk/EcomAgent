"""TraceExporter:Run 事件快照 → Langfuse trace-ingest 结构(计划 04 Task 8)。

映射(trace/span 关联,run_id 为 trace 根):
- ``RUN_*`` 事件 → trace 根项(``trace-create``,``id``/``traceId`` = run_id);
- 其余事件(TOOL_/REQUEST_/BACKEND_/MODEL_/...)→ 根下 span 项
  (``span-create``,observation id = ``{run_id}:{sequence_no}``,可被
  ``event_id`` 覆盖,``parentObservationId`` 指向 trace 根);
- 关联 ID(run/session/attempt/tool_call/sequence)进 ``attributes``,
  事件 payload 作为 ``body.input``;全部经 :class:`TraceRedactor` 字段级
  脱敏后才导出——Secret 不入 Trace。

Langfuse 客户端经构造注入(Protocol),测试注入内存 fake,不发真实网络。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from adapters.langfuse.client import LangfuseClient
from runtime.observability.redaction import TraceRedactor


TRACE_ROOT_PREFIX: Final = "RUN_"
"""``RUN_*`` 事件映射为 trace 根;其余事件映射为其下 span。"""


@dataclass(frozen=True)
class TraceEventSnapshot:
    """AgentEvent 只读快照(可来自 ORM 行或内存构造);脱敏映射的输入。"""

    event_type: str
    run_id: str
    session_id: str
    sequence_no: int
    payload: Mapping[str, Any]
    event_id: str | None = None
    attempt_id: str | None = None
    tool_call_id: str | None = None
    workspace_id: str | None = None
    occurred_at: datetime | None = None


class TraceExporter:
    """把 Run 事件构建为关联 Trace 并经注入的 client 导出;导出前全量脱敏。"""

    def __init__(
        self, client: LangfuseClient, redactor: TraceRedactor | None = None
    ) -> None:
        self._client = client
        self._redactor = redactor if redactor is not None else TraceRedactor()

    def build_payload(self, event: TraceEventSnapshot) -> dict[str, Any]:
        """单条事件 → 脱敏后的 trace-ingest 批项(trace 根或 span)。"""
        redacted_payload = self._redactor.redact(dict(event.payload))
        attributes = self._attributes(event)
        timestamp = _iso_utc(event.occurred_at)
        if event.event_type.startswith(TRACE_ROOT_PREFIX):
            return {
                "type": "trace-create",
                "id": event.run_id,
                "traceId": event.run_id,
                "timestamp": timestamp,
                "attributes": attributes,
                "body": {
                    "id": event.run_id,
                    "traceId": event.run_id,
                    "name": event.event_type,
                    "timestamp": timestamp,
                    "attributes": attributes,
                    "input": redacted_payload,
                },
            }
        observation_id = (
            event.event_id
            if event.event_id is not None
            else f"{event.run_id}:{event.sequence_no}"
        )
        return {
            "type": "span-create",
            "id": observation_id,
            "traceId": event.run_id,
            "timestamp": timestamp,
            "attributes": attributes,
            "body": {
                "id": observation_id,
                "traceId": event.run_id,
                "name": event.event_type,
                "startTime": timestamp,
                "endTime": timestamp,
                "parentObservationId": event.run_id,
                "attributes": attributes,
                "input": redacted_payload,
            },
        }

    async def export_events(self, events: Iterable[TraceEventSnapshot]) -> None:
        """把一批事件构建为 trace 批并经 client.ingest 提交。"""
        await self._client.ingest([self.build_payload(event) for event in events])

    def _attributes(self, event: TraceEventSnapshot) -> dict[str, Any]:
        """Run/Tool/Request 关联 ID 的确定集;None 项不出现。"""
        attributes: dict[str, Any] = {
            "run_id": event.run_id,
            "session_id": event.session_id,
            "sequence_no": event.sequence_no,
            "event_type": event.event_type,
        }
        if event.attempt_id is not None:
            attributes["attempt_id"] = event.attempt_id
        if event.tool_call_id is not None:
            attributes["tool_call_id"] = event.tool_call_id
        if event.workspace_id is not None:
            attributes["workspace_id"] = event.workspace_id
        return attributes


def _iso_utc(value: datetime | None) -> str:
    """ISO-8601 UTC 字符串;None 落为导出时刻。"""
    moment = datetime.now(UTC) if value is None else value
    return moment.astimezone(UTC).isoformat()


__all__ = ["TRACE_ROOT_PREFIX", "TraceEventSnapshot", "TraceExporter"]
