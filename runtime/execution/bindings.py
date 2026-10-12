"""写执行元数据值对象(计划 04 Task 2;架构文档 §3.3)。

``ToolExecutionProfile`` 把"写 Tool 落到哪个平台 Adapter、是否原生幂等、是否
支持回查"三个字段,从 Runtime 的全局配置收敛为**单个 Tool 的静态绑定数据**。

Runtime 与 AgentLoop 不再持有 ``adapter_key`` / ``supports_native_idempotency`` /
``supports_reconciliation`` 全局字段;写执行只读取 Run 钉住的 ToolPin 上携带的
Profile,因此同一 AgentLoop 内可以并存使用不同 Adapter 的写 Tool。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ToolExecutionProfile(BaseModel):
    """单个写 Tool 的执行元数据;不可变值对象,随 ToolBinding 静态绑定。"""

    model_config = ConfigDict(frozen=True)

    adapter_key: str
    supports_native_idempotency: bool = False
    supports_reconciliation: bool = False


__all__ = ["ToolExecutionProfile"]
