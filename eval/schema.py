"""Eval Scenario Schema 与协议指纹(计划 05 Task 1)。

Eval 是 Runtime 外部的 Scenario + Runner + Scorer + Report(计划 05 架构
说明):本模块只定义数据契约与指纹计算,不执行任何评测,也不依赖具体
Runner。Scenario 必须显式声明权威期望(``expected_final_state``)与初始
状态、工作区、执行者——缺失即拒绝构造,防止"模型文本说成功"替代平台
权威状态判分。

协议指纹 ``protocol_fingerprint``:canonical JSON(排序键、紧凑分隔符)
+ SHA-256。纳入 schema 版本、场景全部字段(包括完整消息文本)和全部
``EvalConfig`` 固定字段。任何可能影响模型决策或判分的数据改变都会改变指纹。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eval.capability_manifest import EvalCapabilityManifest
from eval.config import EvalConfig


SCHEMA_VERSION: Final[int] = 4
"""场景协议版本。

v3:补上行为合同——``expected_tool_arguments``(工具实际入参)、
``forbidden_tool_calls``(连尝试都不允许)、``turn_expectations``(逐轮答复),
使"最终状态对"与"过程对"可以分开判。v2 及以前的场景没有这些字段,
它们的指纹因此全部失效:同一题目在新旧协议下不可比。

v4:补上 ``clarification``——缺信息场景不再只认"结构化 Input Request",
口头追问同样可判,判据是"补全前追问过、补全前没写过"。v3 的 Clarification
分区 18 次执行全部被判成协议失败,原因是协议只有一种通过路径。
"""


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


class InventoryState(BaseModel):
    """位置维度库存状态；符号引用跨隔离 Workspace 保持稳定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    location_ref: str = Field(min_length=1)
    available_quantity: int = Field(ge=0)


class InitialState(BaseModel):
    """场景开始前的权威业务初始状态(Mock 平台种子)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listings: tuple[ListingState, ...] = ()
    inventory_levels: tuple[InventoryState, ...] = ()


class ExpectedFinalState(BaseModel):
    """权威最终状态断言:判分只看平台侧状态,不看模型文本。

    ``listings`` 中只填写需要断言的字段(``None`` 表示不断言);
    ``max_logical_writes`` 断言该场景允许的最大逻辑写次数(重复提交
    不得增加),默认 1。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    listings: tuple[ListingState, ...] = ()
    inventory_levels: tuple[InventoryState, ...] = ()
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
    """场景执行者授权模板;缺省无权限,评测夹具按此真实建成员授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    permissions: tuple[str, ...] = ()
    store_scope_mode: Literal["ALL", "SELECTED", "NONE"] = "NONE"
    store_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_store_scope(self) -> ActorSpec:
        if self.store_scope_mode == "SELECTED" and not self.store_ids:
            raise ValueError("SELECTED store scope requires at least one store_id")
        if self.store_scope_mode != "SELECTED" and self.store_ids:
            raise ValueError("store_ids are only valid with SELECTED store scope")
        return self


class UserInteraction(BaseModel):
    """Runner 执行的显式用户交互;值使用店铺等场景符号键。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["input", "confirmation"]
    message_index: int | None = Field(default=None, ge=0)
    values: dict[str, str] = Field(default_factory=dict)
    decision: Literal["accept", "reject"] | None = None

    @model_validator(mode="after")
    def validate_interaction(self) -> UserInteraction:
        if self.kind == "input":
            if not self.values or self.decision is not None:
                raise ValueError("input interaction requires values and no decision")
        elif self.values or self.decision is None:
            raise ValueError("confirmation interaction requires a decision and no values")
        return self


class ToolOrderConstraint(BaseModel):
    """声明必须满足的工具先后关系,不约束其他合法工具调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before: str = Field(min_length=1)
    after: str = Field(min_length=1)


class ResponseFact(BaseModel):
    """需要核验的关键答复事实及其证据来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    source: Literal["tool_result", "final_state"]


class ToolArgumentExpectation(BaseModel):
    """对某个 Tool 实际入参的断言。

    ``field`` 取 Tool 归一化后的模型可见字段名(如 ``target_price`` /
    ``reason`` / ``delta``),``value`` 为期望值。数值按十进制比较,其余按
    去空白后的字面比较。

    ``scope`` 决定这条期望约束几次调用:

    - ``any``(缺省):存在一次调用满足即可。适合"多条路径都可能"的字段;
    - ``all``:**每次**携带该字段的调用都必须满足。用于单一写目标的场景
      ——"先按错价写一次失败,再按对价写一次成功"最终状态是对的,但中间那次
      错价写不该被忽略。

    同一 Tool 同一字段的多条 ``any`` 期望要求分别落在**不同**调用上
    (multiset 匹配),因此两条相反的期望不能被一次调用同时满足。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: str = Field(min_length=1)
    scope: Literal["any", "all"] = "any"


class TurnExpectation(BaseModel):
    """单轮答复断言:某条用户消息之后、下一条用户消息之前的答复。

    ``after_message_index`` 指向 ``Scenario.messages`` 中的用户消息下标;该
    轮答复由这段区间内的全部 Assistant 消息拼接而成。多轮场景下"第一轮说
    对了、第二轮没提"与"两轮都提了"必须能区分开——只取最终答复会把两者
    都算通过。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    after_message_index: int = Field(ge=0)
    expected_facts: tuple[ResponseFact, ...] = ()
    forbidden_facts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_declared(self) -> TurnExpectation:
        if not self.expected_facts and not self.forbidden_facts:
            raise ValueError("turn expectation must declare facts to check")
        return self


class ClarificationRequirement(BaseModel):
    """缺信息场景的判分声明:先追问、后写入。

    两类澄清路径都必须能通过:

    - **结构化追问**:Runtime 抛出 Input Request,Runner 按 ``interactions``
      声明的值应答(原来的唯一路径);
    - **口头追问**:模型直接用自然语言问缺哪个字段,不产生 Input Request。
      这条路径此前被判成"unconsumed user interaction"整例失败——它不是模型
      缺陷,而是协议只有一种表达方式。

    判分不看"问得像不像",只看两条顺序事实:补全前必须有一条带问号的答复,
    补全前不得有任何写入。``missing_fields`` 只用于报告与覆盖矩阵,不参与
    文本匹配(避免把同义写法判失败)。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    missing_fields: tuple[str, ...] = Field(min_length=1)
    allow_natural_language: bool = True


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
    interactions: tuple[UserInteraction, ...] = ()
    faults: tuple[str, ...] = ()
    expected_final_state: ExpectedFinalState
    allowed_side_effects: tuple[SideEffect, ...] = ()
    forbidden_side_effects: tuple[SideEffect, ...] = ()
    allowed_paths: tuple[AllowedPath, ...] = ()
    tool_order_constraints: tuple[ToolOrderConstraint, ...] = ()
    expected_response_facts: tuple[ResponseFact, ...] = ()
    forbidden_response_facts: tuple[str, ...] = ()
    expected_tool_arguments: tuple[ToolArgumentExpectation, ...] = ()
    forbidden_tool_calls: tuple[str, ...] = ()
    turn_expectations: tuple[TurnExpectation, ...] = ()
    clarification: ClarificationRequirement | None = None

    @model_validator(mode="after")
    def validate_interaction_message_indexes(self) -> Scenario:
        indexes = [
            interaction.message_index
            for interaction in self.interactions
            if interaction.message_index is not None
        ]
        if len(indexes) != len(set(indexes)):
            raise ValueError("each user message can satisfy at most one interaction")
        for index in indexes:
            if index >= len(self.messages) or self.messages[index].role != "user":
                raise ValueError("interaction message_index must reference a user message")
        return self

    @model_validator(mode="after")
    def validate_contract_assertions(self) -> Scenario:
        """合同断言必须自洽:轮次指向用户消息,禁止与要求不指向同一 Tool。"""
        for expectation in self.turn_expectations:
            index = expectation.after_message_index
            if index >= len(self.messages) or self.messages[index].role != "user":
                raise ValueError(
                    "turn expectation must reference a user message"
                )
        if len({item.after_message_index for item in self.turn_expectations}) != len(
            self.turn_expectations
        ):
            raise ValueError("each user message can carry at most one turn expectation")
        asserted_tools = {item.tool for item in self.expected_tool_arguments}
        conflicts = asserted_tools & set(self.forbidden_tool_calls)
        if conflicts:
            raise ValueError(
                "tools cannot be both asserted and forbidden: "
                + ", ".join(sorted(conflicts))
            )
        if self.turn_expectations and len(self.messages) < 2:
            raise ValueError("turn expectations require a multi-message scenario")
        return self

    @model_validator(mode="after")
    def validate_clarification(self) -> Scenario:
        """缺信息场景必须真的有第二轮用户补充,且交互只能是供值。"""
        if self.clarification is None:
            return self
        user_indexes = [
            index for index, message in enumerate(self.messages) if message.role == "user"
        ]
        if len(user_indexes) < 2:
            raise ValueError("clarification requires at least two user messages")
        for interaction in self.interactions:
            if interaction.kind != "input":
                raise ValueError("clarification scenarios only accept input interactions")
            if interaction.message_index == user_indexes[0]:
                raise ValueError(
                    "clarification interaction must supply a later user message"
                )
        return self


def _scenario_projection(scenario: Scenario) -> dict[str, object]:
    """指纹用的场景投影:完整场景数据,包含用户措辞与期望断言。"""
    return scenario.model_dump(mode="json")


def _sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _benchmark_config_projection(config: EvalConfig) -> dict[str, object]:
    """评测试卷配置:排除被测 Agent/Prompt/Skill/Tool 的版本。"""
    return {
        "model": config.model,
        "base_url": config.base_url,
        "temperature": config.temperature,
        "thinking_mode": config.thinking_mode,
        "repetitions": config.repetitions,
        "scorer_version": config.scorer_version,
    }


def benchmark_fingerprint(scenario: Scenario, config: EvalConfig) -> str:
    """同一题目、模型、采样和 Scorer 的可比性指纹。"""
    return _sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "scenario": _scenario_projection(scenario),
            "benchmark_config": _benchmark_config_projection(config),
        }
    )


def system_fingerprint(
    manifest: EvalCapabilityManifest, config: EvalConfig | None = None
) -> str:
    """被测系统指纹:冻结能力清单 + 评测时真实调用的模型端点。

    能力清单来自 Runtime 库里的已发布行,不包含评测进程真实使用的 Provider
    (真实模型评测会替换掉库里的 Mock 后端)。只哈希清单会让指纹描述一个
    被替换掉的组件,因此把 ``config.model`` / ``config.base_url`` 一起纳入。
    """
    if config is None:
        return manifest.capability_fingerprint
    return _sha256(
        {
            "capability_fingerprint": manifest.capability_fingerprint,
            "model": config.model,
            "base_url": config.base_url,
        }
    )


def protocol_fingerprint(
    scenario: Scenario,
    config: EvalConfig,
    manifest: EvalCapabilityManifest,
) -> str:
    """计算协议指纹:canonical JSON(排序键)+ SHA-256 十六进制摘要。

    覆盖 ``SCHEMA_VERSION``、场景、Provider/采样配置与冻结能力 Manifest。
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario": _scenario_projection(scenario),
        "config": config.model_dump(mode="json"),
        "capability_manifest": manifest.model_dump(mode="json"),
    }
    return _sha256(payload)
