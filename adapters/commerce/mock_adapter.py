"""确定性 Mock 平台 Adapter(计划 03 Task 5;P1 Task 6 迁移到 PricingGateway)。

权威状态:``product_operations`` 的 ``listings`` 表即 Mock 平台的平台侧
权威状态(架构文档 §3:"Mock Adapter 将这些表视为外部平台的权威状态")。
价格经 :class:`ProductRepository` 的 ``version`` CAS 真正写入 ``listings``,
因此写入结果真实反映持久状态,供上层 ``authoritative_price`` 断言;Adapter
自身只额外维护 ``operation_id`` 幂等去重表(内存),记录每个操作的上次回执
与逻辑写事实,供重复提交回放与 ``count_logical_writes`` 断言。

P1 起本 Adapter 实现 :class:`PricingGateway`(``get_price`` / ``set_price`` /
``reconcile_price``);旧 ``execute`` / ``reconcile`` 保留为兼容包装,委托新方法
并映射回旧回执类型,供历史 ToolVersion 的 Run 恢复。

P1 起本 Adapter 实现 :class:`ListingGateway`(``get_listing`` /
``publish_listing`` / ``unpublish_listing`` / ``reconcile_listing``):刊登发布状态
直接写在 ``listings.status`` 上,取消发布只改状态,不删除 Listing、不清零库存。
目标状态与当前状态相同时按幂等 no-op 返回 ``APPLIED`` + ``changed=False``(零逻辑写、
版本不变);改写与改价、库存同构,只是使用共享的 :class:`WriteReceipt`。

``set_price`` 执行顺序(确定性,无随机):

1. ``timeout_before_write`` → UNKNOWN(请求未达平台逻辑,零状态变更);
2. 幂等去重:已见 ``operation_id`` 且业务意图指纹一致 → 回放上次回执;指纹不同
   (换资源、换目标价/币种/增量/原因)→ FAILED/``IDEMPOTENCY_KEY_REUSED``
   (确定失败,不回放,零逻辑写);
3. ``permanent_failure`` → FAILED(确定失败,请求未达写路径,不建记录);
4. 平台侧命令校验(Listing 存在、币种一致),拒绝 → FAILED 且零逻辑写;
5. ``expected_version`` CAS 写入;零行命中 → FAILED/VERSION_CONFLICT,
   不覆盖新状态;
6. 写后重读 Listing,回执携带观测到的前后价格与最终版本;
7. ``response_lost_after_commit`` → UNKNOWN(写入已生效并已记录,回查
   可确认 APPLIED)。

事务边界:若调用方已有事务,仅 flush 交给调用方;若无活动事务,Mock Adapter
为模拟平台数据库操作开启并提交独立短事务。Adapter 不包含网络请求。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.faults import FaultInjector
from adapters.commerce.protocol import (
    REASON_CURRENCY_MISMATCH,
    REASON_IDEMPOTENCY_CONFLICT,
    REASON_LISTING_NOT_FOUND,
    REASON_PLATFORM_FAILURE,
    REASON_RESPONSE_LOST_AFTER_COMMIT,
    REASON_TIMEOUT_BEFORE_WRITE,
    REASON_VERSION_CONFLICT,
    ExecutionReceipt,
    ExecutionStatus,
    ReconciliationCommand,
    UpdatePriceCommand,
    is_retryable_reason_code,
)
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.inventory.gateway import (
    REASON_INVENTORY_NOT_FOUND,
    AdjustInventoryCommand,
    InventoryLevelSnapshot,
    InventoryQuery,
    InventoryUnavailable,
    InventoryWriteReceipt,
    ReconcileInventoryCommand,
)
from agents.product_operations.inventory.service import (
    OPERATION_TYPE as INVENTORY_OPERATION_TYPE,
)
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingPublicationSnapshot,
    ListingQuery,
    ListingUnavailable,
    ReconcileListingCommand,
)
from agents.product_operations.listing.service import (
    OPERATION_TYPE as LISTING_PUBLICATION_OPERATION_TYPE,
)
from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceSnapshot,
    PriceUnavailable,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.pricing.service import (
    OPERATION_TYPE as PRICING_OPERATION_TYPE,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


@dataclass(frozen=True)
class _OperationRecord:
    """operation_id 幂等记录:上次回执、资源身份、业务意图与逻辑写事实。

    价格、库存与刊登共享同一张按 ``operation_id`` 去重的表:``listing_id`` 与
    ``applied_price`` 供改价回放/回查,``location_id``/``product_id`` 与
    ``applied_quantity`` 供库存调整回放/回查,``listing_id`` 与
    ``applied_publication`` 供刊登发布状态变更回放/回查;未用到的字段为 ``None``。

    改价与刊登作用的资源都是 Listing,因此共享 ``workspace_id + listing_id`` 这个
    资源身份;两者的业务意图不同(各自首元素是不同能力标识,改价还含目标价与币种,
    刊登还含目标发布状态与原因),所以同一个 ``operation_id`` 在两者之间复用时会被
    写路径判为冲突,不会互相回放。

    恢复契约把"同一个资源"与"同一个业务动作"分成两个正交概念,分别服务两条
    路径——只比较资源会让"同一 ``operation_id`` 改了目标价/增量"被错误回放,
    只比较业务意图又会让回查无法只凭资源定位记录:

    - ``resource_identity``(**回查路径**使用):本操作**作用的资源**的规范化
      元组(价格侧 ``workspace_id + listing_id``,库存侧 ``workspace_id +
      location_id + product_id``)。回查时资源身份或预期值不匹配 → ``NOT_APPLIED``
      (fail closed),绝不因价格/数量恰好相同而猜想 ``APPLIED``。
    - ``operation_intent``(**写路径**使用):本操作**业务意图**的规范化指纹,
      首元素是本能力的受控 ``operation_type``(跨能力复用在结构上即冲突),其后
      在资源身份之上并入外部引用与目标值(价格侧再加 ``target_price``/
      ``currency``,库存侧再加 ``delta``/``expected_quantity_after``/``reason``)。
      写路径逐项比对:意图不同 → 确定失败 ``IDEMPOTENCY_KEY_REUSED``,绝不回放
      旧回执;意图相同才按平台幂等契约回放。

    两个指纹都**排除** ``expected_version``:它是"基于哪一次读取"的乐观锁版本,
    同一次重复提交会重读资源而自然 +1,纳入即会把正常重复提交误判成冲突
    (见 ``test_duplicate_submit_writes_listing_version_only_once``)。
    """

    receipt: PriceWriteReceipt | InventoryWriteReceipt | WriteReceipt
    workspace_id: uuid.UUID
    logical_writes: int
    committed: bool
    resource_identity: tuple[str, ...] = ()
    operation_intent: tuple[str, ...] = ()
    listing_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    applied_price: Decimal | None = None
    applied_quantity: int | None = None
    applied_publication: str | None = None


class MockCommerceAdapter:
    """确定性 Mock 平台;结构化实现 :class:`PricingGateway` 与 :class:`InventoryGateway`。

    幂等去重表为内存状态:仅在请求到达平台写路径(第 4 步之后)时创建记录;
    写前超时(UNKNOWN)与持续失败(FAILED)都不产生记录。但"不产生记录"不等于
    "值得重试":两者都带固定 ``reason_code``,重排队只会得到同一结论——按
    Runtime 的确定失败语义,它们的 ToolCall 都落终态,而非自动重试。

    库存调整(``adjust_inventory``)与改价同构:按 ``operation_id`` 幂等去重、
    以 ``version`` CAS 更新数量并保证调整后非负;``reconcile_inventory`` 同时
    校验操作记录与预期调整后数量,权威数量与预期不一致(疑似并发变更)时返回
    ``UNKNOWN``,绝不猜测 ``APPLIED``。

    刊登发布状态变更(``publish_listing`` / ``unpublish_listing``)同样按
    ``operation_id`` 幂等去重、以 ``version`` CAS 更新 ``listings.status``;同目标
    时是幂等 no-op(``APPLIED`` + ``changed=False``、零逻辑写),``reconcile_listing``
    在资源身份或目标状态不一致时 fail closed,绝不因"当前恰好是该状态"判定 APPLIED。

    恢复契约不依赖"上游肯定不会传错":写路径比对**业务意图指纹**
    (``operation_intent``:资源 + 外部引用 + 目标值),回查路径比对**资源身份**
    (``resource_identity``:Workspace + 资源):

    - 写路径:同一 ``operation_id`` 被用于**另一个资源或另一个业务动作**时确定失败
      (``IDEMPOTENCY_KEY_REUSED``),不回放旧回执;意图完全一致才按平台幂等契约
      回放(不比对 ``expected_version`` 等随首次写入自然变化的字段);
    - 回查路径:资源身份或预期值不匹配一律 fail closed(``NOT_APPLIED``),绝不因
      数量/价格恰好相同而猜测 ``APPLIED``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = ProductRepository(session)
        self.faults = FaultInjector()
        self._operations: dict[str, _OperationRecord] = {}

    # ---- PricingGateway ----

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:
        """读取平台侧权威价格;Listing 缺失或币种不符即不可读。"""
        if self._session.in_transaction():
            return await self._get_price(query)
        async with self._session.begin():
            return await self._get_price(query)

    async def _get_price(self, query: PriceQuery) -> PriceSnapshot:
        listing = await self._repository.get_listing(
            query.workspace_id, query.listing_id
        )
        if listing is None or listing.currency != query.currency:
            raise PriceUnavailable("listing is not readable on the platform")
        return PriceSnapshot(
            price=listing.current_price,
            currency=listing.currency,
            listing_version=listing.version,
            external_ref=listing.external_product_ref,
        )

    async def set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """按确定性顺序执行写命令;独立模拟平台事务只覆盖数据库操作。"""
        if self._session.in_transaction():
            return await self._set_price(command)
        async with self._session.begin():
            return await self._set_price(command)

    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        """以权威状态回答;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。

        幂等记录不存在或未提交 → 确定未应用;记录与回查命令的资源身份
        (Workspace/Listing)不一致 → 确定未应用;否则重读 Listing,权威价格
        与期望价格及本操作应用价一致才返回 APPLIED。
        """
        if self._session.in_transaction():
            return await self._reconcile_price(command)
        async with self._session.begin():
            return await self._reconcile_price(command)

    # ---- InventoryGateway ----

    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot:
        """读取平台侧权威库存;库存记录缺失即不可读。"""
        if self._session.in_transaction():
            return await self._get_inventory(query)
        async with self._session.begin():
            return await self._get_inventory(query)

    async def adjust_inventory(self, command: AdjustInventoryCommand) -> InventoryWriteReceipt:
        """按确定性顺序执行库存写命令;独立模拟平台事务只覆盖数据库操作。"""
        if self._session.in_transaction():
            return await self._adjust_inventory(command)
        async with self._session.begin():
            return await self._adjust_inventory(command)

    async def reconcile_inventory(
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        """以权威状态回答库存是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。

        幂等记录不存在或未提交 → 确定未应用;记录与回查命令的资源身份
        (Workspace/位置/商品)或预期调整后数量不一致 → 确定未应用;否则重读
        权威库存:数量仍等于预期 → APPLIED,数量已被并发改动 → UNKNOWN
        (无法区分是本操作还是并发变更所致,绝不猜测 APPLIED)。
        """
        if self._session.in_transaction():
            return await self._reconcile_inventory(command)
        async with self._session.begin():
            return await self._reconcile_inventory(command)

    # ---- ListingGateway ----

    async def get_listing(self, query: ListingQuery) -> ListingPublicationSnapshot:
        """读取平台侧权威发布状态;Listing 缺失或跨 Workspace 即不可读。"""
        if self._session.in_transaction():
            return await self._get_listing(query)
        async with self._session.begin():
            return await self._get_listing(query)

    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """把刊登置为 ``PUBLISHED``;独立模拟平台事务只覆盖数据库操作。"""
        return await self._change_publication(
            command, target_state=ListingStatus.PUBLISHED
        )

    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """把刊登置为 ``UNPUBLISHED``;保留 Listing 与库存,不做任何替代动作。"""
        return await self._change_publication(
            command, target_state=ListingStatus.UNPUBLISHED
        )

    async def reconcile_listing(
        self, command: ReconcileListingCommand
    ) -> ReconcileResult:
        """以权威状态回答刊登变更是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。

        幂等记录不存在或未提交 → 确定未应用;记录与回查命令的资源身份
        (Workspace/Listing)或**目标发布状态**不一致 → 确定未应用;否则重读
        Listing,权威状态仍等于目标才返回 APPLIED。
        """
        if self._session.in_transaction():
            return await self._reconcile_listing(command)
        async with self._session.begin():
            return await self._reconcile_listing(command)

    # ---- 兼容包装(历史 ToolVersion 的 Run 恢复) ----

    async def execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        """旧写入口:委托 :meth:`set_price` 并映射回 :class:`ExecutionReceipt`。"""
        receipt = await self.set_price(_to_set_price_command(command))
        return _to_execution_receipt(receipt, command.idempotency_key)

    async def reconcile(self, command: ReconciliationCommand) -> ReconcileResult:
        """旧回查入口:委托 :meth:`reconcile_price`。"""
        return await self.reconcile_price(
            ReconcilePriceCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                store_id=command.store_id,
                listing_id=command.listing_id,
                external_store_ref=command.external_store_ref,
                external_product_ref=command.external_product_ref,
                expected_price=command.expected_price,
                currency=command.currency,
            )
        )

    async def count_logical_writes(self, operation_id: str) -> int:
        """该 operation_id 已发生的逻辑写次数;未知操作返回 0。"""
        record = self._operations.get(operation_id)
        return 0 if record is None else record.logical_writes

    # ---- 内部 ----

    async def _set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """执行数据库 Mock 平台逻辑(不发起网络请求)。"""
        # 1. 写前超时:请求未达平台逻辑(含去重层),无任何状态变更。
        if self.faults.should_timeout_before_write():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_TIMEOUT_BEFORE_WRITE,
            )

        # 2. 幂等去重:已见 operation_id 时,业务意图指纹一致才回放旧回执;指纹不同
        #    (换了资源、目标价或币种)一律确定失败,绝不把别处的成功回放成本次成功。
        record = self._operations.get(command.operation_id)
        if record is not None:
            if record.operation_intent != _price_operation_intent(command):
                return _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_IDEMPOTENCY_CONFLICT,
                )
            return cast("PriceWriteReceipt", record.receipt)

        # 3. 平台逻辑不可用:确定失败(非 UNKNOWN);未达写路径,不建记录。
        if self.faults.should_fail_permanently():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.FAILED,
                reason_code=REASON_PLATFORM_FAILURE,
            )

        # 4. 平台侧命令校验:Listing 必须存在,币种必须一致。
        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if listing is None:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_LISTING_NOT_FOUND,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )
        if listing.currency != command.currency:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_CURRENCY_MISMATCH,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )

        before_price = listing.current_price

        # 5. expected_version CAS 写入;零行命中即版本过期,不覆盖新状态。
        updated = await self._repository.update_price(
            workspace_id=command.workspace_id,
            listing_id=command.listing_id,
            target_price=command.target_price,
            expected_version=command.expected_version,
        )
        if updated is None:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_VERSION_CONFLICT,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )

        # 6. 写后重读,回执携带观测到的价格与版本。
        reread = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        observed: Listing = reread if reread is not None else updated
        receipt = PriceWriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.APPLIED,
            before_price=before_price,
            after_price=observed.current_price,
            resource_version=observed.version,
            external_ref=observed.external_product_ref,
            changed=True,
        )
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=1,
            committed=True,
            resource_identity=_listing_resource_identity(
                command.workspace_id, command.listing_id
            ),
            operation_intent=_price_operation_intent(command),
            listing_id=command.listing_id,
            applied_price=command.target_price,
        )

        # 7. 响应丢失:写入已生效,调用方只能得到 UNKNOWN,回查恢复。
        if self.faults.should_lose_response_after_commit():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_RESPONSE_LOST_AFTER_COMMIT,
            )
        return receipt

    async def _reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        if self.faults.consume_reconcile_unknown():
            return ReconcileResult.UNKNOWN

        record = self._operations.get(command.operation_id)
        if record is None or not record.committed:
            return ReconcileResult.NOT_APPLIED
        # 回查命令必须与记录指向同一资源;错 Workspace/Listing 一律 fail closed,
        # 不得因目标价格恰好相同就判定 APPLIED。
        if record.resource_identity != _listing_resource_identity(
            command.workspace_id, command.listing_id
        ):
            return ReconcileResult.NOT_APPLIED

        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if (
            listing is not None
            and record.applied_price is not None
            and listing.current_price == command.expected_price
            and command.expected_price == record.applied_price
        ):
            return ReconcileResult.APPLIED
        return ReconcileResult.NOT_APPLIED

    async def _get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot:
        level = await self._repository.get_inventory_level(
            query.workspace_id, query.location_id, query.product_id
        )
        if level is None:
            raise InventoryUnavailable("inventory level is not readable on the platform")
        return InventoryLevelSnapshot(
            available_quantity=level.available_quantity,
            inventory_version=level.version,
            external_ref=query.external_location_ref,
        )

    async def _adjust_inventory(self, command: AdjustInventoryCommand) -> InventoryWriteReceipt:
        """执行数据库 Mock 平台逻辑(不发起网络请求),顺序与改价一致。"""
        # 1. 写前超时:请求未达平台逻辑(含去重层),无任何状态变更。
        if self.faults.should_timeout_before_write():
            return _inventory_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_TIMEOUT_BEFORE_WRITE,
            )

        # 2. 幂等去重:已见 operation_id 时,业务意图指纹一致才回放旧回执;指纹不同
        #    (换了资源、增量或原因)一律确定失败,绝不把别处的成功回放成本次成功。
        record = self._operations.get(command.operation_id)
        if record is not None:
            if record.operation_intent != _inventory_operation_intent(command):
                return _inventory_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_IDEMPOTENCY_CONFLICT,
                )
            return cast("InventoryWriteReceipt", record.receipt)

        # 3. 平台逻辑不可用:确定失败(非 UNKNOWN);未达写路径,不建记录。
        if self.faults.should_fail_permanently():
            return _inventory_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.FAILED,
                reason_code=REASON_PLATFORM_FAILURE,
            )

        # 4. 平台侧命令校验:该 (位置, 商品) 的库存记录必须存在。
        level = await self._repository.get_inventory_level(
            command.workspace_id, command.location_id, command.product_id
        )
        if level is None:
            return self._record_inventory(
                command,
                _inventory_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_INVENTORY_NOT_FOUND,
                ),
                logical_writes=0,
                committed=False,
                applied_quantity=None,
            )

        before_quantity = level.available_quantity

        # 5. expected_version CAS 写入;仓库层同时保证调整后数量非负。
        adjusted = await self._repository.adjust_inventory(
            workspace_id=command.workspace_id,
            inventory_level_id=level.id,
            delta=command.delta,
            expected_version=command.expected_version,
        )
        if adjusted is None:
            return self._record_inventory(
                command,
                _inventory_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_VERSION_CONFLICT,
                ),
                logical_writes=0,
                committed=False,
                applied_quantity=None,
            )

        # 6. 写后重读,回执携带观测到的数量与版本。
        reread = await self._repository.get_inventory_level(
            command.workspace_id, command.location_id, command.product_id
        )
        observed = reread if reread is not None else adjusted
        receipt = InventoryWriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.APPLIED,
            before_quantity=before_quantity,
            after_quantity=observed.available_quantity,
            resource_version=observed.version,
            external_ref=command.external_location_ref,
            changed=True,
        )
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=1,
            committed=True,
            resource_identity=_inventory_resource_identity(
                command.workspace_id, command.location_id, command.product_id
            ),
            operation_intent=_inventory_operation_intent(command),
            location_id=command.location_id,
            product_id=command.product_id,
            applied_quantity=command.expected_quantity_after,
        )

        # 7. 响应丢失:写入已生效,调用方只能得到 UNKNOWN,回查恢复。
        if self.faults.should_lose_response_after_commit():
            return _inventory_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_RESPONSE_LOST_AFTER_COMMIT,
            )
        return receipt

    async def _reconcile_inventory(
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        if self.faults.consume_reconcile_unknown():
            return ReconcileResult.UNKNOWN

        record = self._operations.get(command.operation_id)
        if record is None or not record.committed:
            return ReconcileResult.NOT_APPLIED
        # 回查命令必须与记录指向**同一资源**且预期数量一致;错 Workspace / 位置 /
        # 商品时,即使目标数量碰巧相同也必须 fail closed,不得判定 APPLIED。
        if (
            record.resource_identity
            != _inventory_resource_identity(
                command.workspace_id, command.location_id, command.product_id
            )
            or record.applied_quantity != command.expected_quantity_after
        ):
            return ReconcileResult.NOT_APPLIED

        level = await self._repository.get_inventory_level(
            command.workspace_id, command.location_id, command.product_id
        )
        if level is None:
            return ReconcileResult.NOT_APPLIED
        if level.available_quantity != command.expected_quantity_after:
            # 权威数量与预期不一致:无法区分是本操作还是并发变更所致。
            return ReconcileResult.UNKNOWN
        return ReconcileResult.APPLIED

    async def _get_listing(self, query: ListingQuery) -> ListingPublicationSnapshot:
        listing = await self._repository.get_listing(query.workspace_id, query.listing_id)
        if listing is None:
            raise ListingUnavailable("listing is not readable on the platform")
        return ListingPublicationSnapshot(
            publication_state=listing.status,
            listing_version=listing.version,
            external_ref=listing.external_product_ref,
        )

    async def _change_publication(
        self, command: ChangePublicationCommand, *, target_state: ListingStatus
    ) -> WriteReceipt:
        """执行数据库 Mock 平台逻辑(不发起网络请求),顺序与改价一致。"""
        if self._session.in_transaction():
            return await self._apply_publication(command, target_state=target_state)
        async with self._session.begin():
            return await self._apply_publication(command, target_state=target_state)

    async def _apply_publication(
        self, command: ChangePublicationCommand, *, target_state: ListingStatus
    ) -> WriteReceipt:
        """刊登发布状态变更的确定性执行序列。

        与改价的差别只有"同目标 no-op":目标状态已经等于当前状态时,这次调用
        没有业务意义可执行,按平台幂等语义返回 ``APPLIED`` + ``changed=False``,
        零逻辑写、版本不变——把它当成失败会逼模型反复重试同一个已满足的请求。
        取消发布**只**改发布状态,不删除 Listing,也不清零库存(设计 §4.5)。
        """
        # 1. 写前超时:请求未达平台逻辑(含去重层),无任何状态变更。
        if self.faults.should_timeout_before_write():
            return _write_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_TIMEOUT_BEFORE_WRITE,
            )

        # 2. 幂等去重:已见 operation_id 时,业务意图指纹一致才回放旧回执;指纹不同
        #    (换了资源、目标状态或原因)一律确定失败,绝不把别处的成功回放成本次成功。
        record = self._operations.get(command.operation_id)
        if record is not None:
            if record.operation_intent != _publication_operation_intent(
                command, target_state
            ):
                return _write_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_IDEMPOTENCY_CONFLICT,
                )
            return cast("WriteReceipt", record.receipt)

        # 3. 平台逻辑不可用:确定失败(非 UNKNOWN);未达写路径,不建记录。
        if self.faults.should_fail_permanently():
            return _write_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.FAILED,
                reason_code=REASON_PLATFORM_FAILURE,
            )

        # 4. 平台侧命令校验:Listing 必须存在(跨 Workspace 视为不存在)。
        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if listing is None:
            return self._record_publication(
                command,
                target_state,
                _write_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_LISTING_NOT_FOUND,
                ),
                logical_writes=0,
                committed=False,
                applied_publication=None,
            )

        # 4b. 同目标幂等 no-op:没有可执行的变更,按成功返回并标记 changed=False。
        if listing.status is target_state:
            return self._record_publication(
                command,
                target_state,
                WriteReceipt(
                    operation_id=command.operation_id,
                    idempotency_key=command.idempotency_key,
                    status=WriteStatus.APPLIED,
                    resource_version=listing.version,
                    external_ref=listing.external_product_ref,
                    changed=False,
                ),
                logical_writes=0,
                committed=True,
                applied_publication=target_state.value,
            )

        # 5. expected_version CAS 写入;零行命中即版本过期,不覆盖新状态。
        updated = await self._repository.set_listing_publication_state(
            workspace_id=command.workspace_id,
            listing_id=command.listing_id,
            target_state=target_state,
            expected_version=command.expected_version,
        )
        if updated is None:
            return self._record_publication(
                command,
                target_state,
                _write_receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_VERSION_CONFLICT,
                ),
                logical_writes=0,
                committed=False,
                applied_publication=None,
            )

        # 6. 写后重读,回执携带观测到的版本。
        reread = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        observed = reread if reread is not None else updated
        receipt = WriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.APPLIED,
            resource_version=observed.version,
            external_ref=observed.external_product_ref,
            changed=True,
        )
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=1,
            committed=True,
            resource_identity=_listing_resource_identity(
                command.workspace_id, command.listing_id
            ),
            operation_intent=_publication_operation_intent(command, target_state),
            listing_id=command.listing_id,
            applied_publication=target_state.value,
        )

        # 7. 响应丢失:写入已生效,调用方只能得到 UNKNOWN,回查恢复。
        if self.faults.should_lose_response_after_commit():
            return _write_receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_RESPONSE_LOST_AFTER_COMMIT,
            )
        return receipt

    async def _reconcile_listing(
        self, command: ReconcileListingCommand
    ) -> ReconcileResult:
        if self.faults.consume_reconcile_unknown():
            return ReconcileResult.UNKNOWN

        record = self._operations.get(command.operation_id)
        if record is None or not record.committed:
            return ReconcileResult.NOT_APPLIED
        # 回查命令必须与记录指向同一资源**且目标发布状态一致**;错 Workspace /
        # Listing 或换目标时一律 fail closed,不得因"当前恰好是该状态"判定 APPLIED。
        if record.resource_identity != _listing_resource_identity(
            command.workspace_id, command.listing_id
        ):
            return ReconcileResult.NOT_APPLIED
        if record.applied_publication != command.expected_state.value:
            return ReconcileResult.NOT_APPLIED

        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if listing is None or listing.status is not command.expected_state:
            return ReconcileResult.NOT_APPLIED
        return ReconcileResult.APPLIED

    def _record_publication(
        self,
        command: ChangePublicationCommand,
        target_state: ListingStatus,
        receipt: WriteReceipt,
        *,
        logical_writes: int,
        committed: bool,
        applied_publication: str | None,
    ) -> WriteReceipt:
        """登记刊登幂等记录并返回回执(回执与记录所见一致,供回放)。"""
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=logical_writes,
            committed=committed,
            resource_identity=_listing_resource_identity(
                command.workspace_id, command.listing_id
            ),
            operation_intent=_publication_operation_intent(command, target_state),
            listing_id=command.listing_id,
            applied_publication=applied_publication,
        )
        return receipt

    def _record_inventory(
        self,
        command: AdjustInventoryCommand,
        receipt: InventoryWriteReceipt,
        *,
        logical_writes: int,
        committed: bool,
        applied_quantity: int | None,
    ) -> InventoryWriteReceipt:
        """登记库存幂等记录并返回回执(回执与记录所见一致,供回放)。"""
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=logical_writes,
            committed=committed,
            resource_identity=_inventory_resource_identity(
                command.workspace_id, command.location_id, command.product_id
            ),
            operation_intent=_inventory_operation_intent(command),
            location_id=command.location_id,
            product_id=command.product_id,
            applied_quantity=applied_quantity,
        )
        return receipt

    def _record(
        self,
        command: SetListingPriceCommand,
        receipt: PriceWriteReceipt,
        *,
        logical_writes: int,
        committed: bool,
        applied_price: Decimal | None,
    ) -> PriceWriteReceipt:
        """登记幂等记录并返回回执(回执与记录所见一致,供回放)。"""
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            logical_writes=logical_writes,
            committed=committed,
            resource_identity=_listing_resource_identity(
                command.workspace_id, command.listing_id
            ),
            operation_intent=_price_operation_intent(command),
            listing_id=command.listing_id,
            applied_price=applied_price,
        )
        return receipt


def _listing_resource_identity(
    workspace_id: uuid.UUID, listing_id: uuid.UUID
) -> tuple[str, ...]:
    """Listing 类写操作作用的**资源身份**:Workspace + Listing(回查路径唯一依据)。

    改价与刊登发布状态变更作用在同一类资源上,因此共用本函数;只证明"是不是同一个
    资源",是否同一业务动作由 :func:`_price_operation_intent` /
    :func:`_publication_operation_intent` 判定。
    """
    return (str(workspace_id), str(listing_id))


def _inventory_resource_identity(
    workspace_id: uuid.UUID, location_id: uuid.UUID, product_id: uuid.UUID
) -> tuple[str, ...]:
    """库存操作作用的**资源身份**:Workspace + 位置 + 商品(回查路径唯一依据)。"""
    return (str(workspace_id), str(location_id), str(product_id))


def _price_operation_intent(command: SetListingPriceCommand) -> tuple[str, ...]:
    """改价**业务意图指纹**:能力标识 + 资源 + 外部引用 + ``target_price`` + ``currency``。

    首元素是本能力的受控 ``operation_type``(``SET_LISTING_PRICE``):三个能力共享
    同一张 ``operation_id`` 去重表,显式能力前缀让"跨能力复用同一个 ``operation_id``"
    在**结构上**就是冲突,而不依赖元组剩余位置的取值域恰好不相交。

    写路径据此判定"同一个 ``operation_id`` 是不是同一个业务动作":同一 Listing
    上改不同的价、或换币种,都是**不同命令**,必须确定失败而不是回放旧成功。
    刻意排除 ``expected_version``——重复提交会重读资源使版本自然 +1,那属于
    **同一次**操作,纳入会把正常重复提交误判成冲突。
    """
    return (
        PRICING_OPERATION_TYPE,
        *_listing_resource_identity(command.workspace_id, command.listing_id),
        command.external_store_ref,
        command.external_product_ref,
        str(command.target_price),
        command.currency,
    )


def _inventory_operation_intent(command: AdjustInventoryCommand) -> tuple[str, ...]:
    """库存**业务意图指纹**:能力标识 + 资源 + 外部引用 + ``delta`` + 预期结果 + ``reason``。

    首元素是本能力的受控 ``operation_type``(``ADJUST_INVENTORY``),理由与改价一致。
    ``expected_quantity_after`` 由写前持久化的 ``quantity_before`` 与 ``delta``
    确定,重复提交时保持不变;调整原因属业务语义,同样不能排除。排除
    ``expected_version`` 的理由与改价一致。
    """
    return (
        INVENTORY_OPERATION_TYPE,
        *_inventory_resource_identity(
            command.workspace_id, command.location_id, command.product_id
        ),
        command.external_store_ref,
        command.external_location_ref,
        command.external_product_ref,
        str(command.delta),
        str(command.expected_quantity_after),
        command.reason,
    )


def _publication_operation_intent(
    command: ChangePublicationCommand, target_state: ListingStatus
) -> tuple[str, ...]:
    """刊登发布状态变更的**业务意图指纹**:能力标识 + 资源 + 外部引用 + 目标状态 + ``reason``。

    首元素是本能力的受控 ``operation_type``(``CHANGE_LISTING_PUBLICATION``)。

    目标状态是"这个业务动作要什么"的核心,原因属业务语义,都不能排除——否则同一个
    ``operation_id`` 在同一 Listing 上"发布 → 取消发布"或换一个下架原因会被错误
    回放。排除 ``expected_version`` 的理由与改价、库存一致(见
    :class:`_OperationRecord`)。

    改价与刊登作用的资源都是 Listing,指纹中段因此同形;但首元素能力标识不同,
    所以同一个 ``operation_id`` 跨能力复用一定会被判为冲突,不依赖后续字段的取值域。
    """
    return (
        LISTING_PUBLICATION_OPERATION_TYPE,
        *_listing_resource_identity(command.workspace_id, command.listing_id),
        command.external_store_ref,
        command.external_product_ref,
        target_state.value,
        command.reason or "",
    )


def _write_receipt(
    operation_id: str,
    idempotency_key: str,
    status: WriteStatus,
    *,
    reason_code: str,
) -> WriteReceipt:
    """构造无观测的刊登写回执(UNKNOWN/FAILED 只带 reason_code)。"""
    return WriteReceipt(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
    )


def _receipt(
    operation_id: str,
    idempotency_key: str,
    status: WriteStatus,
    *,
    reason_code: str,
) -> PriceWriteReceipt:
    """构造无观测的价格回执(UNKNOWN/FAILED 只带 reason_code)。"""
    return PriceWriteReceipt(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
    )


def _inventory_receipt(
    operation_id: str,
    idempotency_key: str,
    status: WriteStatus,
    *,
    reason_code: str,
) -> InventoryWriteReceipt:
    """构造无观测的库存回执(UNKNOWN/FAILED 只带 reason_code)。"""
    return InventoryWriteReceipt(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
    )


def _to_set_price_command(command: UpdatePriceCommand) -> SetListingPriceCommand:
    """旧写命令 → PricingGateway 写命令(字段一一对应,``sku`` 不参与平台判定)。"""
    return SetListingPriceCommand(
        operation_id=command.operation_id,
        idempotency_key=command.idempotency_key,
        workspace_id=command.workspace_id,
        store_id=command.store_id,
        listing_id=command.listing_id,
        external_store_ref=command.external_store_ref,
        external_product_ref=command.external_product_ref,
        sku="",
        target_price=command.target_price,
        currency=command.currency,
        expected_version=command.expected_version,
    )


def _to_execution_receipt(
    receipt: PriceWriteReceipt, idempotency_key: str
) -> ExecutionReceipt:
    """价格回执 → 旧 :class:`ExecutionReceipt`(幂等键沿用旧命令派生)。"""
    return ExecutionReceipt(
        operation_id=receipt.operation_id,
        idempotency_key=idempotency_key,
        status=ExecutionStatus(receipt.status.value),
        before_price=receipt.before_price,
        after_price=receipt.after_price,
        listing_version=receipt.resource_version,
        external_ref=receipt.external_ref,
        reason_code=receipt.reason_code,
        retryable=is_retryable_reason_code(receipt.reason_code),
    )


if TYPE_CHECKING:
    from adapters.commerce.protocol import CommerceAdapter
    from agents.product_operations.inventory.gateway import InventoryGateway
    from agents.product_operations.listing.gateway import ListingGateway
    from agents.product_operations.pricing.gateway import PricingGateway

    _protocol_conformance: type[CommerceAdapter] = MockCommerceAdapter
    _pricing_conformance: type[PricingGateway] = MockCommerceAdapter
    _inventory_conformance: type[InventoryGateway] = MockCommerceAdapter
    _listing_conformance: type[ListingGateway] = MockCommerceAdapter


__all__ = ["MockCommerceAdapter"]
