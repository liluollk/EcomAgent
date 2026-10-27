"""真实模型 driver:按 Workspace 的模型配置解析 Backend。

执行器在哪个入口启动都**常驻**,能不能推进由数据决定:该 Workspace 是否配了
可用的模型连接(以及已发布的价格策略)。没有配置不是"这个进程没装执行器",
而是一次明确的、可展示给用户的失败——页面据此提示"未配置模型",而不是让
Run 无声地停在 QUEUED。

配置来源是用户在配置页写入的那条连接(``model_connections`` +
``model_config_versions`` + ``secrets``),不是 provisioning 的占位行:
后者没有 ``secret_id``,拿不到 API Key,无法真正调用模型。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.model_backends.anthropic_backend import AnthropicBackend
from adapters.model_backends.openai_backend import OpenAIBackend
from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_STABLE_KEY,
    InvalidPricingPolicyConfig,
    parse_pricing_policy_config,
)
from apps.answered_view import rebuild_answered_view
from apps.assembly import ProductApplication, build_product_application
from composition.provisioning.product_operations import (
    MODEL_CONNECTION_KEY,
    ModelConnectionProvisioningSpec,
    ProductOperationsProvisioner,
)
from runtime.backend.protocol import AgentBackend
from runtime.execution.executor import AdvanceOutcome, RunClaim
from runtime.persistence.base import ConfigVersionStatus, ModelBackendType
from runtime.persistence.models.configuration import (
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.execution import Run
from runtime.persistence.models.integrations import Secret
from runtime.security.secrets import EncryptedSecret, SecretCipher, build_secret_binding
from runtime.workspace.service import SECRET_KIND_MODEL_API_KEY


MODEL_NOT_CONFIGURED: Final = "MODEL_NOT_CONFIGURED"
"""Workspace 没有任何带 API Key 的模型连接。"""

PRICING_POLICY_NOT_CONFIGURED: Final = "PRICING_POLICY_NOT_CONFIGURED"
"""Workspace 没有已发布的价格策略。"""


class WorkspaceNotReady(RuntimeError):
    """Workspace 缺少执行所需配置;``code`` 是可直接展示的稳定错误码。

    配置缺失不是暂时性故障:重试同一份配置不会变好。``retryable = False``
    让执行器直接判失败,而不是把 Run 反复退回 QUEUED 直到耗尽预算。
    """

    retryable: bool = False

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkspaceModelConfig:
    """一条可用的模型连接(最新版本 + 明文 API Key)。"""

    version_id: uuid.UUID
    connection_id: uuid.UUID
    stable_key: str
    version: int
    backend_type: ModelBackendType
    base_url: str
    model: str
    api_key: str
    non_secret_options: Mapping[str, Any]


@dataclass(frozen=True)
class WorkspaceReadiness:
    """Workspace 的执行前置条件;缺失项用于页面提示与提交前拒绝。"""

    model: bool
    pricing_policy: bool

    @property
    def ready(self) -> bool:
        return self.model and self.pricing_policy

    @property
    def failure(self) -> WorkspaceNotReady | None:
        """按"先模型、后策略"的顺序返回第一个缺失项。"""
        if not self.model:
            return WorkspaceNotReady(
                MODEL_NOT_CONFIGURED,
                "该工作区还没有可用的模型连接，请先在配置中添加模型和 API Key",
            )
        if not self.pricing_policy:
            return WorkspaceNotReady(
                PRICING_POLICY_NOT_CONFIGURED,
                "该工作区还没有已发布的价格策略，改价前需要在配置中发布一份",
            )
        return None


# ---------------------------------------------------------------------------
# 读取配置
# ---------------------------------------------------------------------------


async def load_workspace_model_config(
    engine: AsyncEngine, workspace_id: uuid.UUID, *, version_id: uuid.UUID | None = None
) -> WorkspaceModelConfig:
    """读取该 Workspace 的可用模型配置;没有则抛 :class:`WorkspaceNotReady`。

    ``version_id`` 由调用方传入时读取**该版本**(Run 创建时已固定),否则取最新
    可用版本。不传版本的路径把"有 secret"的配置排在前面:provisioning 写入的
    占位行没有 ``secret_id``,拿不到 Key,不能当作可用配置。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        if version_id is None:
            statement = (
                select(ModelConfigVersion, ModelConnection)
                .join(
                    ModelConnection,
                    (ModelConnection.workspace_id == ModelConfigVersion.workspace_id)
                    & (ModelConnection.id == ModelConfigVersion.model_connection_id),
                )
                .where(ModelConfigVersion.workspace_id == workspace_id)
                .order_by(
                    ModelConfigVersion.secret_id.is_(None),
                    ModelConfigVersion.version.desc(),
                )
                .limit(1)
            )
        else:
            statement = (
                select(ModelConfigVersion, ModelConnection)
                .join(
                    ModelConnection,
                    (ModelConnection.workspace_id == ModelConfigVersion.workspace_id)
                    & (ModelConnection.id == ModelConfigVersion.model_connection_id),
                )
                .where(
                    ModelConfigVersion.workspace_id == workspace_id,
                    ModelConfigVersion.id == version_id,
                )
                .limit(1)
            )
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise WorkspaceNotReady(
                MODEL_NOT_CONFIGURED,
                "该工作区还没有可用的模型连接，请先在配置中添加模型和 API Key",
            )
        config_row, connection = row
        if config_row.secret_id is None:
            raise WorkspaceNotReady(
                MODEL_NOT_CONFIGURED,
                "该工作区的模型连接还没有 API Key，请先在配置中补全",
            )
        secret_row = (
            await session.scalars(
                select(Secret).where(
                    Secret.workspace_id == workspace_id,
                    Secret.id == config_row.secret_id,
                )
            )
        ).one_or_none()
        if secret_row is None:
            raise WorkspaceNotReady(
                MODEL_NOT_CONFIGURED,
                "该工作区的模型连接引用的 API Key 已不存在，请重新填写",
            )
        api_key = _decrypt_api_key(
            workspace_id=workspace_id,
            base_url=config_row.base_url,
            secret_row=secret_row,
        )
        return WorkspaceModelConfig(
            version_id=config_row.id,
            connection_id=connection.id,
            stable_key=connection.stable_key,
            version=config_row.version,
            backend_type=config_row.backend_type,
            base_url=config_row.base_url,
            model=config_row.model,
            api_key=api_key,
            non_secret_options=dict(config_row.non_secret_options or {}),
        )
    finally:
        await session.close()


def _decrypt_api_key(
    *,
    workspace_id: uuid.UUID,
    base_url: str,
    secret_row: Secret,
) -> str:
    """按写入时的 associated data 还原明文 Key;失配即视为配置不可用。"""
    binding = build_secret_binding(
        workspace_id=str(workspace_id),
        connection_type=SECRET_KIND_MODEL_API_KEY,
        normalized_endpoint=secret_row.endpoint_binding or base_url,
    )
    encrypted = EncryptedSecret(
        key_version=secret_row.key_version,
        nonce=secret_row.nonce,
        ciphertext=secret_row.ciphertext,
        tag=secret_row.auth_tag or b"",
    )
    try:
        plaintext = SecretCipher().decrypt(encrypted, binding=binding)
    except Exception as error:
        raise WorkspaceNotReady(
            MODEL_NOT_CONFIGURED,
            "该工作区的 API Key 无法解密，请重新填写一次",
        ) from error
    return plaintext.decode("utf-8")


async def load_published_pricing_config(
    engine: AsyncEngine, workspace_id: uuid.UUID
) -> PricePolicyConfig:
    """读取该 Workspace 最新发布的价格策略;没有则抛 :class:`WorkspaceNotReady`。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        version_row = (
            await session.scalars(
                select(BusinessPolicyVersion)
                .join(
                    BusinessPolicy,
                    (BusinessPolicy.workspace_id == BusinessPolicyVersion.workspace_id)
                    & (BusinessPolicy.id == BusinessPolicyVersion.business_policy_id),
                )
                .where(
                    BusinessPolicy.workspace_id == workspace_id,
                    BusinessPolicy.stable_key == PRICING_POLICY_STABLE_KEY,
                    BusinessPolicyVersion.status == ConfigVersionStatus.PUBLISHED,
                )
                .order_by(BusinessPolicyVersion.version.desc())
                .limit(1)
            )
        ).first()
        if version_row is None:
            raise WorkspaceNotReady(
                PRICING_POLICY_NOT_CONFIGURED,
                "该工作区还没有已发布的价格策略，改价前需要在配置中发布一份",
            )
        return parse_pricing_policy_config(version_row.config_json)
    finally:
        await session.close()


async def check_workspace_readiness(
    engine: AsyncEngine, workspace_id: uuid.UUID
) -> WorkspaceReadiness:
    """一次性判断两个前置条件,供提交前校验与页面提示使用。"""
    model_ready = True
    try:
        await load_workspace_model_config(engine, workspace_id)
    except WorkspaceNotReady:
        model_ready = False
    policy_ready = True
    try:
        await load_published_pricing_config(engine, workspace_id)
    except (WorkspaceNotReady, InvalidPricingPolicyConfig):
        policy_ready = False
    return WorkspaceReadiness(model=model_ready, pricing_policy=policy_ready)


# ---------------------------------------------------------------------------
# 构造 Backend
# ---------------------------------------------------------------------------


def build_backend(config: WorkspaceModelConfig) -> AgentBackend:
    """按 ``backend_type`` 构造真实后端;SDK 客户端在此创建,不外泄到调用方。"""
    if config.backend_type is ModelBackendType.OPENAI:
        from openai import AsyncOpenAI

        openai_client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        return OpenAIBackend(
            openai_client,
            config.model,
            extra_body=dict(config.non_secret_options) or None,
        )
    if config.backend_type is ModelBackendType.ANTHROPIC:
        from anthropic import AsyncAnthropic

        anthropic_client = AsyncAnthropic(api_key=config.api_key, base_url=config.base_url)
        return AnthropicBackend(anthropic_client, config.model)
    raise WorkspaceNotReady(
        MODEL_NOT_CONFIGURED,
        f"不支持的模型后端类型: {config.backend_type}",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


BackendFactory = Callable[[WorkspaceModelConfig], AgentBackend]


class WorkspaceModelDriver:
    """执行器 driver:每次推进按 Workspace 的模型配置组装商品应用。

    与脚本 driver 的区别只在 Backend 的来源:这里用该 Workspace 配置的模型,
    而不是写死的脚本序列。缓存键带上配置版本号,改了模型配置会重新组装。
    """

    requires_model_config: bool = True
    """声明本 driver 依赖 Workspace 的模型配置;路由据此在提交前给出明确拒绝。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        executor_id: str = "api-executor",
        backend_factory: BackendFactory = build_backend,
    ) -> None:
        self._engine = engine
        self._executor_id = executor_id
        self._backend_factory = backend_factory
        self._applications: dict[
            tuple[uuid.UUID, uuid.UUID, uuid.UUID], ProductApplication
        ] = {}

    @property
    def applications(self) -> Mapping[tuple[uuid.UUID, uuid.UUID, uuid.UUID], ProductApplication]:
        return self._applications

    async def aclose(self) -> None:
        for application in self._applications.values():
            await application.aclose()
        self._applications.clear()

    async def advance(
        self, claim: RunClaim, *, answered: Mapping[str, str] | None = None
    ) -> AdvanceOutcome:
        version_id = await self._run_model_version(claim.workspace_id, claim.run_id)
        config = await load_workspace_model_config(
            self._engine, claim.workspace_id, version_id=version_id
        )
        application = await self._application_for(claim, config)
        if answered is None:
            session = AsyncSession(self._engine, expire_on_commit=False)
            try:
                answered = await rebuild_answered_view(
                    session, claim.workspace_id, claim.run_id
                )
            finally:
                await session.close()
        return await application.advance(claim, answered=answered)

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            run = (
                await session.scalars(
                    select(Run).where(Run.workspace_id == workspace_id, Run.id == run_id)
                )
            ).one_or_none()
            if run is None:
                return False
            actor_id = run.created_by_actor_id
            membership_id = run.created_by_membership_id
            model_version_id = run.model_config_version_id
        finally:
            await session.close()
        try:
            config = await load_workspace_model_config(
                self._engine, workspace_id, version_id=model_version_id
            )
        except WorkspaceNotReady:
            return False
        claim = RunClaim(
            run_id=run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
            attempt_id=uuid.uuid4(),
            attempt_no=0,
        )
        application = await self._application_for(claim, config)
        return await application.reconcile(run_id, workspace_id)

    async def _run_model_version(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Run 创建时固定的模型配置版本;读不到时退回"最新可用版本"。"""
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            return await session.scalar(
                select(Run.model_config_version_id).where(
                    Run.workspace_id == workspace_id, Run.id == run_id
                )
            )
        finally:
            await session.close()

    async def _application_for(
        self, claim: RunClaim, config: WorkspaceModelConfig
    ) -> ProductApplication:
        key = (claim.workspace_id, claim.actor_id, config.version_id)
        application = self._applications.get(key)
        if application is not None:
            return application
        pricing_policy = await load_published_pricing_config(self._engine, claim.workspace_id)
        capabilities = await ProductOperationsProvisioner(
            lambda: AsyncSession(self._engine, expire_on_commit=False)
        ).provision(
            workspace_id=claim.workspace_id,
            model=ModelConnectionProvisioningSpec(
                stable_key=MODEL_CONNECTION_KEY,
                backend_type=config.backend_type,
                base_url=config.base_url,
                model=config.model,
                non_secret_options=dict(config.non_secret_options),
            ),
            pricing_policy=pricing_policy,
        )
        application = await build_product_application(
            self._engine,
            capabilities=capabilities,
            backend=self._backend_factory(config),
            workspace_id=claim.workspace_id,
            actor_id=claim.actor_id,
            membership_id=claim.membership_id,
            executor_id=self._executor_id,
        )
        self._applications[key] = application
        return application


__all__ = [
    "MODEL_NOT_CONFIGURED",
    "PRICING_POLICY_NOT_CONFIGURED",
    "WorkspaceModelConfig",
    "WorkspaceModelDriver",
    "WorkspaceNotReady",
    "WorkspaceReadiness",
    "build_backend",
    "check_workspace_readiness",
    "load_published_pricing_config",
    "load_workspace_model_config",
]
