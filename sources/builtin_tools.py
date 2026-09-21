"""内置平台 API 工具 — 默认收敛为「多平台商品调价控制层」。

定位（Task 2 之后）：

默认 Agent 只暴露三个工具，构成调价闭环 + 元技能：
  1. query_product_snapshot(platform, product_id, sku_id)
       商品快照入口，走 get_price_platform(platform).query_snapshot(ProductRef)，
       返回当前价 / 库存 / 商品状态 / 进行中活动 / 是否活动锁价；未知 SKU 优雅降级。
  2. update_price(platform, product_id, sku_id, target_price, operation_id="")
       提交目标价，走 PriceChangeCoordinator 完成「快照→成本保护→审批→写入→回查」，
       返回 LLM 可读文本。审批已在工具执行前由引擎（RBAC / 模式门 / 业务规则 + ASK
       挂起）完成——工具能跑即已获批准，协调器不再二次询问，但会在审计里留下
       approval_decided（见 PriceChangeCoordinator 注释），不伪装成「协调器自己在问」。
  3. save_skill — 元技能，落盘 SKILL.md，不变。

verify_price_change / query_price_constraints 不作为模型可见 Tool：它们是协调器
内部能力（execution/price_change.py 的 verify() / precheck() 步骤）。

扩展工具（原 11 个操作里其余能力：query_inventory / create_promotion /
query_order_status / product_shelf / service_ticket / query_order_stats /
query_anomalies / query_promotions / query_after_sales_stats / query_knowledge_base）
通过独立入口 get_extension_definitions() / get_extension_handlers() /
get_extension_tool_policies() 暴露，不参与默认 Agent，也不出现在默认
get_definitions() 里。

handler 契约：async **kwargs → str，异常归一为可解释文本，不向 Agent 抛裸异常。
"""

from __future__ import annotations

import contextvars
import os
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional

from execution.policy.timeout import TimeoutError as ToolTimeoutError
from execution.policy.validation import ValidationError
from integrations.commerce.client import RestApiError
from integrations.commerce.models import PromotionRequest
from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceError,
    PriceErrorCode,
    ProductRef,
    get_price_platform,
)
from integrations.commerce.provider import get_commerce_provider
from execution.price_change import PriceChangeCoordinator, PriceChangeState
from permission.tool_policy import ToolPolicy, get_default_tool_policies, get_extension_tool_policies
from session.operation_store import OperationStore

_ACTION_CN = {"on": "上架", "off": "下架"}

# ----------------------------------------------------------------------
# 操作审计会话上下文（引擎在调用调价工具前注入当前会话，供按 session 隔离落盘）
# ----------------------------------------------------------------------
# 定义在 builtin_tools 内，由 agent_core.base_agent 在工具执行前 set；
# 缺失时（如单测直接调用 handler）退化为以 operation_id 作为 session 维度。
_PRICE_SESSION_CTX: contextvars.ContextVar = contextvars.ContextVar("price_session", default=None)


# ----------------------------------------------------------------------
# 异常 → LLM 可读文本
# ----------------------------------------------------------------------

def _format_exec_error(exc: Exception) -> str:
    """执行期异常 → LLM 可理解的错误文本（平台错误/超时/上游畸形）。"""
    if isinstance(exc, RestApiError):
        return f"[平台错误 {exc.code}] {exc.message}"
    if isinstance(exc, ToolTimeoutError):
        return f"[执行超时] {exc}"
    if isinstance(exc, ValidationError):
        return f"[上游响应异常] {exc.message}"
    return f"[执行异常] {exc}"


def _format_price_error(exc: PriceError) -> str:
    """领域层 PriceError → 可解释拦截文本（含错误分类与原因）。"""
    return f"拦截：调价未执行。原因：[{exc.code.value}] {exc.message}"


def _brief_price_error(exc: PriceError) -> str:
    """快照查询失败时给 Agent 的简短说明（不抛裸异常）。"""
    return f"[{exc.code.value}] {exc.message}"


def _dec(value: float) -> Decimal:
    """float → Decimal（领域接口入参），经 str 规避二进制浮点尾差。"""
    return Decimal(str(value))


# ----------------------------------------------------------------------
# shop_id 解析与 ProductRef 构造
# ----------------------------------------------------------------------

def _resolve_shop_id(platform: str) -> str:
    """从默认渠道注册表找 platform 匹配的已启用渠道，用其 name 作为 shop_id。

    找不到（例如离线 mock 配置把渠道 platform 标成 mock）时回退 "default"——
    平台 Adapter 在信封内自行决定鉴权与店铺字段，shop_id 仅作审计/路由提示，
    不进入领域命令的平台私有字段。
    """
    try:
        from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY  # 延迟导入避免循环

        for ch in DEFAULT_CHANNEL_REGISTRY.list():
            if ch.get("platform") == platform and ch.get("enabled"):
                return str(ch["name"])
    except Exception:
        # 注册表不可用时安全回退，不阻断调价
        pass
    return "default"


def _build_ref(platform: str, product_id: str, sku_id: str) -> ProductRef:
    return ProductRef(
        platform=platform,
        shop_id=_resolve_shop_id(platform),
        product_id=product_id,
        sku_id=sku_id,
    )


# ----------------------------------------------------------------------
# 默认工具 handler（调价闭环 + 元技能）
# ----------------------------------------------------------------------

async def query_product_snapshot(platform: str, product_id: str, sku_id: str) -> str:
    """商品快照入口：当前价 / 库存 / 商品状态 / 进行中活动 / 是否活动锁价。

    未知 SKU / 平台不支持：优雅降级为可解释文本，不向 Agent 抛裸异常。
    """
    try:
        ref = _build_ref(platform, product_id, sku_id)
        snapshot = await get_price_platform(platform).query_snapshot(ref)
    except PriceError as exc:
        return f"平台 {platform} 商品 {product_id}/{sku_id} 暂不可查询：{_brief_price_error(exc)}"
    except Exception as exc:  # noqa: BLE001 - 归一为可读文本
        return f"平台 {platform} 商品 {product_id}/{sku_id} 查询失败：{exc}"
    activity = f"；进行中活动：{snapshot.activity_name}" if snapshot.activity_name else "；无进行中活动"
    lock = "（活动锁价，提交改价可能被拦截）" if snapshot.activity_locked else ""
    return (
        f"平台 {platform} 商品 {product_id}/{sku_id}："
        f"当前价 {snapshot.current_price} 元，库存 {snapshot.stock} 件，"
        f"状态 {snapshot.status}{activity}{lock}"
    )


async def update_price(
    platform: str,
    product_id: str,
    sku_id: str,
    target_price: float,
    operation_id: str = "",
) -> str:
    """提交目标价，走 PriceChangeCoordinator 完成调价闭环。

    approval 语义：引擎的 PreToolUse（RBAC / 模式门 / 业务规则）+ ASK 挂起已经
    发生在工具执行**之前**——工具能跑到这里即说明已获批准。因此协调器不再二次
    询问用户，而是用 always-approve 的 approver 推进，并在审计里留下
    approval_decided（由 coordinator.decide_approval 落盘）。这不是「协调器自己
    在问」，而是把引擎已完成的审批决定写回操作审计，供恢复/对账使用。

    operation_id 由引擎在权限检查前注入工具输入（见 agent_core.base_agent），
    与 permission_request 事件携带的是同一个；handler 原样传给协调器，保证
    「权限事件 == 协调器」的贯通。
    """
    op_id = operation_id or _fallback_operation_id()
    session = _PRICE_SESSION_CTX.get()
    session_id = getattr(session, "session_id", "") or op_id
    try:
        ref = _build_ref(platform, product_id, sku_id)
        command = PriceChangeCommand(
            operation_id=op_id,
            product_ref=ref,
            target_price=Decimal(str(target_price)),
            requester=_requester(session),
            reason="",
        )
        coordinator = PriceChangeCoordinator(
            platform=get_price_platform(platform),
            session_id=session_id,
            operation_store=_make_operation_store(),
        )
        # 引擎已审批：always-approve，仅用于在审计里落 approval_decided。
        op = await coordinator.execute(command, approver=lambda _event: True)
    except PriceError as exc:
        return _format_price_error(exc)
    except ValueError as exc:
        return f"[参数无效] {exc}"
    except Exception as exc:  # noqa: BLE001 - 归一为可读文本，不让 Agent 循环抛异常
        return f"[调价失败] {exc}"
    return _format_coordinator_result(op)


async def save_skill(name: str, description: str, body: str, keywords: Optional[list] = None) -> str:
    """保存新的运营技能（SKILL.md 知识包）——skill-creator 元技能的落盘工具。

    校验 name/description/body → 经技能注册表写入用户技能目录（data/skills/），
    mtime 热加载即时生效；失败返回可解释错误文本（不抛异常）。
    """
    import re

    from sources.skill_registry import DEFAULT_SKILL_REGISTRY, Skill

    name = (name or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        return (
            f"[保存失败] 技能标识 {name!r} 不合法：需小写字母开头，"
            "仅含小写字母 / 数字 / 下划线，长度 2-41"
        )
    if not (description or "").strip():
        return "[保存失败] description 必填（用于技能菜单展示与意图识别）"
    if not (body or "").strip():
        return "[保存失败] SOP 正文必填（操作手册 / 平台规则内容）"
    try:
        DEFAULT_SKILL_REGISTRY.add(Skill(
            name=name,
            description=description.strip(),
            keywords=[str(k) for k in (keywords or [])],
            body=body.strip(),
        ))
    except ValueError as exc:
        return f"[保存失败] {exc}"
    return (
        f"技能 {name} 已创建（SKILL.md 已持久化，热加载即时生效）。"
        f"当前技能菜单：{'、'.join(DEFAULT_SKILL_REGISTRY.list_menu_names())}"
    )


# ----------------------------------------------------------------------
# 扩展工具 handler（不参与默认 Agent，仅供扩展通道）
# ----------------------------------------------------------------------

async def query_inventory(channel: str, sku: str) -> str:
    """旧名兼容入口：走旧 CommerceProvider 库存查询（扩展工具，不参与默认 Agent）。

    与默认 query_product_snapshot 不同，本 handler 仍走旧的领域接口；保留它仅为
    向后兼容旧调用与评测，新链路统一经 query_product_snapshot → 调价执行面。
    """
    try:
        r = await get_commerce_provider(channel).query_inventory(sku)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    return f"渠道 {channel} 商品 {sku} 库存 {r.stock} 件：{r.name}"


async def create_promotion(channel: str, sku: str, discount: float, start_time: str, end_time: str) -> str:
    try:
        r = await get_commerce_provider(channel).create_promotion(
            PromotionRequest(
                sku_id=sku, discount=discount, start_time=start_time, end_time=end_time, channel=channel
            )
        )
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    replay = "（幂等：重复请求，未重复创建）" if r.idempotent_replay else ""
    return f"渠道 {channel} 商品 {sku} 已创建 {r.discount * 100}% 折扣促销，时间: {r.start_time} ~ {r.end_time}{replay}"


async def query_order_status(channel: str, order_id: str) -> str:
    try:
        r = await get_commerce_provider(channel).query_order_status(order_id)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    return f"渠道 {channel} 订单 {order_id} 状态: {r.status}"


async def product_shelf(channel: str, sku: str, action: str) -> str:
    """走 CommerceProvider 领域接口；写操作由策略层自动携带幂等键。"""
    try:
        r = await get_commerce_provider(channel).product_shelf(sku, action)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    replay = "（幂等：重复请求，未重复执行）" if r.extra.get("idempotent_replay") else ""
    return f"渠道 {channel} 商品 {sku} 已{_ACTION_CN.get(r.action.value, r.action.value)}{replay}"


async def service_ticket(channel: str, order_id: str, issue: str, priority: str = "normal") -> str:
    try:
        r = await get_commerce_provider(channel).create_service_ticket(
            order_id, issue, channel=channel, priority=priority
        )
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    replay = "（幂等：重复请求，未重复创建）" if r.idempotent_replay else ""
    return (
        f"已创建售后工单 {r.ticket_id}"
        f"（{channel} 订单 {order_id}，优先级 {priority}）：{issue}{replay}"
    )


async def query_order_stats(channel: str, period: str = "近7天") -> str:
    try:
        r = await get_commerce_provider(channel).query_order_stats(period=period)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    return (
        f"{channel} 渠道 {r.period} 订单 {r.orders} 单，"
        f"GMV {r.gmv} 元，客单价 {r.avg} 元"
    )


async def query_anomalies(channel: str) -> str:
    try:
        r = await get_commerce_provider(channel).query_anomalies()
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    detail = "：" + "；".join(r.items) if r.items else "，经营状态正常"
    return f"{channel} 渠道异常项：{len(r.items)} 个{detail}"


async def query_promotions(channel: str) -> str:
    try:
        r = await get_commerce_provider(channel).query_promotions()
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    names = "、".join(i.get("name", "") for i in r.items)
    return f"{channel} 渠道进行中的促销：{names if names else '无'}"


async def query_after_sales_stats(channel: str, period: str = "近7天") -> str:
    try:
        r = await get_commerce_provider(channel).query_after_sales_stats(period=period)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    return (
        f"{channel} 渠道 {r.period} 退款率 {r.refund_rate * 100:.1f}%，"
        f"售后工单 {r.tickets} 单"
    )


async def query_knowledge_base(topic: str) -> str:
    try:
        r = await get_commerce_provider().query_knowledge_base(topic)
    except (RestApiError, ToolTimeoutError, ValidationError) as e:
        return _format_exec_error(e)
    if r.matched:
        return f"[知识库] {r.key}：{r.text}"
    return f"[知识库] 未找到「{topic}」相关条目，可补充完善知识库文档"


# ----------------------------------------------------------------------
# 协调器结果 / 上下文辅助
# ----------------------------------------------------------------------

def _fallback_operation_id() -> str:
    return f"op-{uuid.uuid4().hex[:16]}"


def _requester(session: Any) -> str:
    """从会话用户角色推导 requester；无会话时记为 agent。"""
    if session is not None:
        user = getattr(session, "user", None)
        if isinstance(user, dict) and user.get("role"):
            return f"role:{user['role']}"
    return "agent"


def _make_operation_store() -> OperationStore:
    """操作审计落盘目录：与 session 存储同一根（AGENT_STORAGE_DIR 或 data/sessions）。"""
    base_dir = os.environ.get("AGENT_STORAGE_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "sessions")
    )
    return OperationStore(base_dir)


def _format_coordinator_result(op: Any) -> str:
    """把协调器终态翻译成 LLM 可读文本，覆盖成功/拦截/拒绝/未知。"""
    state: PriceChangeState = op.state
    ref: ProductRef = op.command.product_ref
    target: Decimal = op.command.target_price
    label = f"平台 {ref.platform} 商品 {ref.product_id}/{ref.sku_id}"
    if state == PriceChangeState.SUCCEEDED:
        # 成功：含「已更新」与「回查一致」
        return f"{label} 价格已更新为 {target} 元（平台回查一致）"
    if state == PriceChangeState.BLOCKED:
        # 拦截：含「拦截」+ 原因（成本保护 / 平台不支持 / 平台错误）
        if op.receipt is not None:
            # 写入已被平台接受（有回执）但后续回查没能确认：
            # 这时说「未执行」是错的——价格很可能已经生效，必须如实报告为「未确认」。
            return (
                f"调价结果未确认：平台写入已返回，但回查未完成"
                f"（{op.error or '回查失败'}）。建议重新查询当前价确认实际状态。"
            )
        return f"拦截：调价未执行。原因：{op.error or '不满足调价前置条件'}"
    if state == PriceChangeState.REJECTED:
        # 拒绝 / 未生效：含「未执行」或「未生效」
        return f"调价未生效（未执行）：{op.error or '审批拒绝或回查不一致'}"
    # 其它状态（理论上 execute 不会停在中间态）：解释当前状态，不抛异常
    return f"调价状态 {state.value}：{op.error or '处理中，请稍后回查'}"


# ----------------------------------------------------------------------
# 工具定义（JSON Schema 与 handler 签名逐字对齐；扩展集见下方）
# ----------------------------------------------------------------------

_DEFAULT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_product_snapshot",
        "description": "查询指定平台商品的当前快照：当前价、库存、商品状态、进行中活动与是否活动锁价。改价前先查它。",
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "平台标识：taobao / douyin。"},
                "product_id": {"type": "string", "description": "商品 ID（如 ITEM-1001）。"},
                "sku_id": {"type": "string", "description": "SKU ID（如 SKU-001）。"},
            },
            "required": ["platform", "product_id", "sku_id"],
        },
    },
    {
        "name": "update_price",
        "description": (
            "提交目标价，触发调价闭环（快照→成本保护→审批→平台写入→回查）。"
            "写操作，需审批；成功后以平台回查价为准，低于成本价会被拦截。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "平台标识：taobao / douyin。"},
                "product_id": {"type": "string", "description": "商品 ID（如 ITEM-1001）。"},
                "sku_id": {"type": "string", "description": "SKU ID（如 SKU-001）。"},
                "target_price": {"type": "number", "description": "目标价格（元）；低于成本价会被拦截。"},
            },
            "required": ["platform", "product_id", "sku_id", "target_price"],
        },
    },
    {
        "name": "save_skill",
        "description": (
            "保存一个新的运营技能（SKILL.md 知识包）：name 为小写下划线标识，"
            "description 一句话描述，keywords 触发关键词，body 为 SOP 操作手册 / "
            "平台规则正文。写操作，需权限确认后落盘。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能标识（小写字母开头，可含数字/下划线）。"},
                "description": {"type": "string", "description": "一句话技能描述。"},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "触发关键词列表。",
                },
                "body": {"type": "string", "description": "SOP 正文（markdown）。"},
            },
            "required": ["name", "description", "body"],
        },
    },
]

_EXTENSION_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_inventory",
        "description": "查询指定渠道商品库存（旧名兼容入口，扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}, "sku": {"type": "string", "description": "商品 SKU。"}},
            "required": ["channel", "sku"],
        },
    },
    {
        "name": "create_promotion",
        "description": "在指定渠道创建促销活动（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"},
                "sku": {"type": "string", "description": "商品 SKU。"},
                "discount": {"type": "number", "description": "折扣率（0.0-1.0）。"},
                "start_time": {"type": "string", "description": "开始时间。"},
                "end_time": {"type": "string", "description": "结束时间。"},
            },
            "required": ["channel", "sku", "discount", "start_time", "end_time"],
        },
    },
    {
        "name": "query_order_status",
        "description": "查询指定渠道订单的当前状态（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}, "order_id": {"type": "string", "description": "订单号。"}},
            "required": ["channel", "order_id"],
        },
    },
    {
        "name": "product_shelf",
        "description": "商品上下架操作（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"},
                "sku": {"type": "string", "description": "商品 SKU。"},
                "action": {"type": "string", "description": "on=上架 / off=下架。"},
            },
            "required": ["channel", "sku", "action"],
        },
    },
    {
        "name": "service_ticket",
        "description": "创建售后工单（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"},
                "order_id": {"type": "string", "description": "关联订单号。"},
                "issue": {"type": "string", "description": "问题描述。"},
                "priority": {"type": "string", "description": "优先级，low / normal / high。", "default": "normal"},
            },
            "required": ["channel", "order_id", "issue"],
        },
    },
    {
        "name": "query_order_stats",
        "description": "订单/销售分析（订单量、GMV、客单价）（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}, "period": {"type": "string", "description": "统计周期。", "default": "近7天"}},
            "required": ["channel"],
        },
    },
    {
        "name": "query_anomalies",
        "description": "经营异常排查（价格/库存/评分）（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}},
            "required": ["channel"],
        },
    },
    {
        "name": "query_promotions",
        "description": "促销活动检查（进行中活动）（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}},
            "required": ["channel"],
        },
    },
    {
        "name": "query_after_sales_stats",
        "description": "售后分析（退款率、工单量）（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "渠道标识，可选 taobao / douyin。"}, "period": {"type": "string", "description": "统计周期。", "default": "近7天"}},
            "required": ["channel"],
        },
    },
    {
        "name": "query_knowledge_base",
        "description": "经营知识库查询（话术/政策/规范）（扩展工具）。",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "知识主题，如\"退款政策\"\"上架规范\"。"}},
            "required": ["topic"],
        },
    },
]

_DEFAULT_HANDLERS: dict[str, Callable[..., Any]] = {
    "query_product_snapshot": query_product_snapshot,
    "update_price": update_price,
    "save_skill": save_skill,
}

_EXTENSION_HANDLERS: dict[str, Callable[..., Any]] = {
    "query_inventory": query_inventory,
    "create_promotion": create_promotion,
    "query_order_status": query_order_status,
    "product_shelf": product_shelf,
    "service_ticket": service_ticket,
    "query_order_stats": query_order_stats,
    "query_anomalies": query_anomalies,
    "query_promotions": query_promotions,
    "query_after_sales_stats": query_after_sales_stats,
    "query_knowledge_base": query_knowledge_base,
}

# 扩展工具名清单（断言用）
EXTENSION_TOOL_NAMES: list[str] = [d["name"] for d in _EXTENSION_DEFINITIONS]

# 工具安全策略不放进发给模型的 JSON Schema，避免把平台治理字段暴露给模型后端。
# 未出现在这里的工具（例如未声明元数据的 MCP 工具）由权限管线默认拒绝。


def get_definitions() -> list[dict[str, Any]]:
    """默认内置工具定义列表（OpenAI function-calling 形状）。"""
    return [dict(d) for d in _DEFAULT_DEFINITIONS]


# 运营常用工具集：默认调价闭环 + 库存/促销/订单读 + 高风险写（上下架/建促）
# 由环境变量 AGENT_TOOL_SET=ops 启用（见 transport.state._build_tools）
OPS_TOOL_NAMES: list[str] = [
    "query_product_snapshot",
    "update_price",
    "save_skill",
    "query_inventory",
    "query_promotions",
    "query_order_status",
    "product_shelf",
    "create_promotion",
]


def get_ops_definitions() -> list[dict[str, Any]]:
    """运营工具集定义（默认 ∪ 扩展中被 OPS_TOOL_NAMES 命中的子集）。"""
    wanted = set(OPS_TOOL_NAMES)
    out = [dict(d) for d in _DEFAULT_DEFINITIONS if d["name"] in wanted]
    out.extend(dict(d) for d in _EXTENSION_DEFINITIONS if d["name"] in wanted)
    return out


def get_ops_handlers() -> dict[str, Callable[..., Any]]:
    """运营工具集 handler 映射。"""
    wanted = set(OPS_TOOL_NAMES)
    merged = {**_DEFAULT_HANDLERS, **_EXTENSION_HANDLERS}
    return {k: v for k, v in merged.items() if k in wanted}


def get_handlers() -> dict[str, Callable[..., Any]]:
    """默认内置工具 handler 映射（**kwargs → str，async 安全）。"""
    return dict(_DEFAULT_HANDLERS)


def get_tool_policies() -> dict[str, ToolPolicy]:
    """返回默认工具的平台安全策略（query_product_snapshot / update_price / save_skill / load_skill）。"""
    return get_default_tool_policies()


def get_builtin_tool_policies() -> dict[str, ToolPolicy]:
    """默认 ∪ 扩展策略（供权限管线在启用 ops/full 工具集时使用）。"""
    from permission.tool_policy import get_builtin_tool_policies as _all

    return _all()


def get_extension_definitions() -> list[dict[str, Any]]:
    """扩展内置工具定义列表（不参与默认 Agent）。"""
    return [dict(d) for d in _EXTENSION_DEFINITIONS]


def get_extension_handlers() -> dict[str, Callable[..., Any]]:
    """扩展内置工具 handler 映射（**kwargs → str，async 安全）。"""
    return dict(_EXTENSION_HANDLERS)


def get_extension_tool_policies() -> dict[str, ToolPolicy]:
    """返回扩展工具的平台安全策略。"""
    return get_extension_tool_policies()


def tool_names() -> list[str]:
    """默认内置工具名列表（测试与文档用）。"""
    return [d["name"] for d in _DEFAULT_DEFINITIONS]
