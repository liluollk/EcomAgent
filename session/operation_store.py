"""调价操作记录的 JSONL 持久化 — 按 session 隔离、可重建。

OperationStore 把一次调价操作的全部审计事件追加写入 JSONL：

    operation_created     操作建立（含命令视图与初始状态）
    state_changed         状态转移（from/to/reason）
    approval_decided      审批决定（approved / rejected + 规则摘要）
    platform_attempt      平台写入尝试（outcome / idempotency_key）
    verification_observed 回查观察（consistent / expected / observed）

写入习惯与 session.storage 一致：append 模式逐行 JSON，每次写后 flush + fsync，
保证中断后可从磁盘重建操作当前状态。

目录约定：沿用 session.storage 的「存储根目录 + 会话文件」风格，但单独放在
operations/ 子目录下（每 session 一个文件 {session_id}.jsonl），避免与会话
消息审计文件混用，同时共享同一个 base_dir。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from integrations.commerce.price_models import PriceChangeCommand, ProductRef
from execution.price_change import PriceChangeOperation, PriceChangeState


class OperationStore:
    """按 session 隔离的调价操作 JSONL 记录器。"""

    def __init__(self, base_dir: str) -> None:
        """base_dir 为存储根目录（与 session.storage 共用同一根）。"""
        self.base_dir = base_dir

    # ------------------------------------------------------------------
    # 路径与写入
    # ------------------------------------------------------------------
    def _path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, "operations", f"{session_id}.jsonl")

    def append(self, session_id: str, record: dict[str, Any]) -> None:
        """追加一条操作事件记录（append + fsync 风格）。"""
        ops_dir = os.path.join(self.base_dir, "operations")
        os.makedirs(ops_dir, exist_ok=True)
        payload = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        path = self._path(session_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ------------------------------------------------------------------
    # 读取与重建
    # ------------------------------------------------------------------
    def load(self, session_id: str) -> list[dict]:
        """读取某 session 的全部操作事件记录（按写入顺序）。"""
        path = self._path(session_id)
        records: list[dict] = []
        if not os.path.exists(path):
            return records
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    def operations_for_session(self, session_id: str) -> list[str]:
        """该 session 下出现的全部 operation_id（按首次出现顺序）。"""
        seen: list[str] = []
        for rec in self.load(session_id):
            oid = rec.get("operation_id", "")
            if oid and oid not in seen:
                seen.append(oid)
        return seen

    def rebuild_operation(self, session_id: str, operation_id: str) -> Optional[PriceChangeOperation]:
        """从 JSONL 重建某操作的当前状态（含命令与状态轨迹）。

        当前状态取最后一条指向该 operation_id 的 state_changed / operation_created
        的目标状态；命令取自 operation_created 记录。
        """
        records = [r for r in self.load(session_id) if r.get("operation_id") == operation_id]
        if not records:
            return None

        created = next((r for r in records if r.get("type") == "operation_created"), None)
        if created is None:
            return None

        command = _command_from_view(created)
        current_state: Optional[PriceChangeState] = None
        trail: list[str] = []

        for rec in records:
            if rec.get("type") == "state_changed":
                to_state = rec.get("to_state")
                if to_state:
                    current_state = PriceChangeState(to_state)
                    trail.append(to_state)

        # 尚无 state_changed 时，退回 operation_created 的目标状态
        if current_state is None:
            current_state = PriceChangeState(created.get("to_state", "CREATED"))

        op = PriceChangeOperation(
            operation_id=operation_id,
            command=command,
            state=current_state,
            idempotency_key=f"price_{operation_id}",
            trail=trail,
        )
        # 回填审批决定文本（若存在）
        decided = next((r for r in records if r.get("type") == "approval_decided"), None)
        if decided is not None:
            op.rule_summary = decided.get("rule_summary", op.rule_summary)
        return op


def _command_from_view(view: dict[str, Any]) -> PriceChangeCommand:
    """从 operation_created 的可序列化视图还原 PriceChangeCommand。"""
    ref = ProductRef(
        platform=view.get("platform", ""),
        shop_id=view.get("shop_id", ""),
        product_id=view.get("product_id", ""),
        sku_id=view.get("sku_id", ""),
    )
    return PriceChangeCommand(
        operation_id=view.get("operation_id", ""),
        product_ref=ref,
        target_price=view.get("target_price", "0"),
        requester=view.get("requester", ""),
        reason=view.get("reason", ""),
    )


__all__ = ["OperationStore"]
