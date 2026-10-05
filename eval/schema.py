"""Eval Scenario Schema 与协议指纹(计划 05 Task 1)。

Eval 是 Runtime 外部的 Scenario + Runner + Scorer + Report(计划 05 架构
说明):本模块只定义数据契约与指纹计算,不执行任何评测,也不依赖具体
Runner。Scenario 必须显式声明权威期望(``expected_final_state``)与初始
状态、工作区、执行者——缺失即拒绝构造,防止"模型文本说成功"替代平台
权威状态判分。

协议指纹 ``protocol_fingerprint``:canonical JSON(排序键、紧凑分隔符)
+ SHA-256。纳入 schema 版本、场景结构字段(含 id/category/risk_level/
initial_state/workspace/actor/角色结构/faults/expected_final_state/副作用
断言/允许路径)与全部 ``EvalConfig`` 固定字段;消息自由文本(措辞)不
纳入——措辞属于表达层润色,不影响协议可比性,角色结构与其他场景字段
变化仍会改变指纹。
"""

import hashlib
import json
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eval.config import EvalConfig


SCHEMA_VERSION: Final[int] = 1


class Message(BaseModel):
    """场景对话消息;``role`` 受限,``content`` 为自由文本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1)


class ListingState(BaseModel):
    """单条 Listing 的状态(初始种子或权威期望断言)。

    ``price``/``currency``/``status`` 为 ``None`` 表示该字段不断言(初始
    状态中即"不提供");金额用字符串承载十进制语义,JSON 序列化确定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    price: Decimal | None = None
    currency: str | None = None
    status: str | None = None


class InitialState(BaseModel):
    """场景开始前的权威业务初始状态(Mock 平台种子)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listings: tuple[ListingState, ...] = ()


class ExpectedFinalState(BaseModel):
    """权威最终状态断言:判分只看平台侧状态,不看模型文本。

    ``listings`` 中只填写需要断言的字段(``None`` 表示不断言);
    ``max_logical_writes`` 断言该场景允许的最大逻辑写次数(重复提交
    不得增加),默认 1。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    listings: tuple[ListingState, ...] = ()
    max_logical_writes: int = Field(default=1, ge=0)


class SideEffect(BaseModel):
    """一条副作用断言:哪个稳定 Tool 键作用于哪个目标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    target: str = Field(min_length=1)


class AllowedPath(BaseModel):
    """一条允许的执行路径断言(如回查路径 ``("UNKNOWN", "APPLIED")``)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    steps: tuple[str, ...] = ()


class WorkspaceSpec(BaseModel):
    """为场景创建的独立工作区模板。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)


class ActorSpec(BaseModel):
    """场景执行者模板(角色由服务端授权,非前端输入)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)


class Scenario(BaseModel):
    """版本化 Eval 场景:一次评测的最小完整定义。

    必填字段缺失(尤其是 ``expected_final_state`` 权威期望)即拒绝构造;
    未知字段一律拒绝,防止数据集静默漂移。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]
    initial_state: InitialState
    workspace: WorkspaceSpec
    actor: ActorSpec
    messages: tuple[Message, ...] = Field(min_length=1)
    faults: tuple[str, ...] = ()
    expected_final_state: ExpectedFinalState
    allowed_side_effects: tuple[SideEffect, ...] = ()
    forbidden_side_effects: tuple[SideEffect, ...] = ()
    allowed_paths: tuple[AllowedPath, ...] = ()


def _scenario_projection(scenario: Scenario) -> dict[str, object]:
    """指纹用的场景投影:全部结构字段,剔除消息自由文本(仅保留角色)。"""
    data: dict[str, Any] = scenario.model_dump(mode="json")
    data["messages"] = [{"role": message["role"]} for message in data["messages"]]
    return data


def protocol_fingerprint(scenario: Scenario, config: EvalConfig) -> str:
    """计算协议指纹:canonical JSON(排序键)+ SHA-256 十六进制摘要。

    覆盖 ``SCHEMA_VERSION``、场景结构投影(见 ``_scenario_projection``)与
    ``EvalConfig`` 全部字段;同一指纹即可比,指纹不同即协议漂移。
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario": _scenario_projection(scenario),
        "config": config.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
