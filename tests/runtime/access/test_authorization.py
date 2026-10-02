"""授权不变式测试(计划 01 Task 5 Step 1;架构文档 §2、§4 补充)。

第一个用例为计划文档原文;其余为架构文档写明的不变式:
权限交集允许、缺动作权限拒绝、ALL/SELECTED scope 语义、JWT claim 集合
(sub/iat/exp/jti,超集即失败)、过期与篡改 token 拒绝、argon2 密码往返、
以及 ActorContext 从 PostgreSQL 当前库态构建(角色/权限/范围不在 token 中)。
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.access.actor import ActorContextBuilder, ActorContextError
from runtime.access.authorization import Decision
from runtime.access.passwords import hash_password, verify_password
from runtime.access.tokens import (
    LOCAL_TEST_SECRET,
    TokenRejected,
    issue_token,
    resolve_secret,
    verify_token,
)
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import AccountStatus, MembershipRole
from runtime.persistence.models.identity import MembershipPermission


TEST_SECRET = "access-tests-jwt-secret-0123456789abcdef"
REQUEST_ID = "req-authorization-tests"
OPERATOR_PERMISSIONS = frozenset(
    {"session.create", "run.read", "product.read", "product.price.write"}
)


# ---------------------------------------------------------------------------
# 授权判定
# ---------------------------------------------------------------------------


async def test_write_requires_action_and_resource_scope(authorizer, operator) -> None:
    """计划原文:operator 有写权限但 store-2 不在其资源范围内。"""
    decision = authorizer.authorize(
        actor=operator,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset({("store", "store-2")}),
    )
    assert decision.allowed is False
    assert decision.reason_code == "RESOURCE_SCOPE_DENIED"


async def test_permission_intersection_allows_scoped_resource(authorizer, operator) -> None:
    """架构文档 §4:角色 ∩ 资源范围 ∩ 动作权限同时命中时放行。"""
    decision = authorizer.authorize(
        actor=operator,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset({("store", "store-1")}),
    )
    assert decision.allowed is True
    assert decision.reason_code is None


async def test_missing_action_permission_is_denied(authorizer, operator) -> None:
    """架构文档 §3.2:operator 无 membership.manage,缺动作权限即拒绝。"""
    decision = authorizer.authorize(
        actor=operator,
        required_permissions=frozenset({"membership.manage"}),
        resources=frozenset(),
    )
    assert decision.allowed is False
    assert decision.reason_code == "PERMISSION_DENIED"


async def test_read_operation_without_resources_only_requires_permission(
    authorizer, operator
) -> None:
    """只读调用不传资源引用,仅做动作权限校验。"""
    decision = authorizer.authorize(
        actor=operator,
        required_permissions=frozenset({"run.read"}),
        resources=frozenset(),
    )
    assert decision.allowed is True
    assert decision.reason_code is None


async def test_all_scope_mode_allows_any_resource_id(authorizer, owner) -> None:
    """架构文档 §4.1:ALL 模式放行该资源类型的任意 id。"""
    decision = authorizer.authorize(
        actor=owner,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset({("store", "store-98")}),
    )
    assert decision.allowed is True
    assert decision.reason_code is None


async def test_resource_type_without_scope_is_denied(authorizer, owner) -> None:
    """资源类型无 scope 映射时一律拒绝。"""
    decision = authorizer.authorize(
        actor=owner,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset({("warehouse", "wh-1")}),
    )
    assert decision.allowed is False
    assert decision.reason_code == "RESOURCE_SCOPE_DENIED"


def test_decision_is_frozen_and_forbids_unknown_fields() -> None:
    decision = Decision(allowed=False, reason_code="PERMISSION_DENIED")
    with pytest.raises(ValidationError):
        decision.allowed = True
    with pytest.raises(ValidationError):
        Decision(allowed=True, unexpected="x")


# ---------------------------------------------------------------------------
# ActorContext 构建(当前 PostgreSQL 状态)
# ---------------------------------------------------------------------------


async def test_actor_context_is_built_from_current_database_state(
    db_session: AsyncSession, identity_seed
) -> None:
    """角色、权限与资源范围来自当前库态,而不是 token 或请求输入。"""
    context = await ActorContextBuilder(db_session).build(
        actor_id=identity_seed.operator_user.id,
        workspace_id=identity_seed.workspace.id,
        request_id=REQUEST_ID,
    )
    assert context.actor_id == identity_seed.operator_user.id
    assert context.workspace_id == identity_seed.workspace.id
    assert context.membership_id == identity_seed.operator_membership.id
    assert context.role == MembershipRole.OPERATOR
    assert context.permissions == OPERATOR_PERMISSIONS
    assert context.resource_scopes == {
        "store": ResourceScope(mode="SELECTED", resource_ids=frozenset({"store-1"}))
    }
    assert context.request_id == REQUEST_ID


async def test_actor_context_reflects_database_updates(
    db_session: AsyncSession, identity_seed
) -> None:
    """同一事务内库态变化后重新构建,权限随当前数据变化。"""
    builder = ActorContextBuilder(db_session)
    before = await builder.build(
        actor_id=identity_seed.operator_user.id,
        workspace_id=identity_seed.workspace.id,
        request_id=REQUEST_ID,
    )
    db_session.add(
        MembershipPermission(
            workspace_id=identity_seed.workspace.id,
            membership_id=identity_seed.operator_membership.id,
            permission="membership.manage",
        )
    )
    await db_session.flush()
    after = await builder.build(
        actor_id=identity_seed.operator_user.id,
        workspace_id=identity_seed.workspace.id,
        request_id=REQUEST_ID,
    )
    assert "membership.manage" not in before.permissions
    assert "membership.manage" in after.permissions


async def test_builder_rejects_unknown_membership(db_session: AsyncSession, identity_seed) -> None:
    with pytest.raises(ActorContextError):
        await ActorContextBuilder(db_session).build(
            actor_id=uuid.uuid4(),
            workspace_id=identity_seed.workspace.id,
            request_id=REQUEST_ID,
        )


async def test_builder_rejects_membership_from_other_workspace(
    db_session: AsyncSession, identity_seed
) -> None:
    """成员身份按 (workspace_id, user_id) 查询,跨 workspace 不可见。"""
    with pytest.raises(ActorContextError):
        await ActorContextBuilder(db_session).build(
            actor_id=identity_seed.operator_user.id,
            workspace_id=uuid.uuid4(),
            request_id=REQUEST_ID,
        )


async def test_builder_rejects_disabled_membership(
    db_session: AsyncSession, identity_seed
) -> None:
    """架构文档 §6:Membership 已禁用时拒绝执行。"""
    identity_seed.operator_membership.status = AccountStatus.DISABLED
    await db_session.flush()
    with pytest.raises(ActorContextError):
        await ActorContextBuilder(db_session).build(
            actor_id=identity_seed.operator_user.id,
            workspace_id=identity_seed.workspace.id,
            request_id=REQUEST_ID,
        )


# ---------------------------------------------------------------------------
# JWT(claim 仅 sub/iat/exp/jti)
# ---------------------------------------------------------------------------


def test_jwt_claims_are_exactly_sub_iat_exp_jti() -> None:
    token = issue_token(uuid.uuid4(), secret=TEST_SECRET)
    claims = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
    assert set(claims) == {"sub", "iat", "exp", "jti"}


def test_verify_token_round_trip() -> None:
    now = datetime.now(UTC)
    actor_id = uuid.uuid4()
    token = issue_token(
        actor_id, secret=TEST_SECRET, now=now, expires_in=timedelta(minutes=5)
    )
    verified = verify_token(token, secret=TEST_SECRET)
    assert verified.actor_id == actor_id
    assert verified.issued_at == now.replace(microsecond=0)
    assert verified.expires_at == (now + timedelta(minutes=5)).replace(microsecond=0)
    assert verified.token_id


def test_expired_token_is_rejected() -> None:
    issued_at = datetime.now(UTC) - timedelta(minutes=10)
    token = issue_token(
        uuid.uuid4(), secret=TEST_SECRET, now=issued_at, expires_in=timedelta(minutes=5)
    )
    with pytest.raises(TokenRejected):
        verify_token(token, secret=TEST_SECRET)


def test_tampered_token_is_rejected() -> None:
    token = issue_token(uuid.uuid4(), secret=TEST_SECRET)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}aa.{signature}"
    with pytest.raises(TokenRejected):
        verify_token(tampered, secret=TEST_SECRET)


def test_token_signed_with_other_secret_is_rejected() -> None:
    token = issue_token(uuid.uuid4(), secret="another-secret-value-0123456789abcdef")
    with pytest.raises(TokenRejected):
        verify_token(token, secret=TEST_SECRET)


def test_alg_none_token_is_rejected() -> None:
    """算法固定 HS256:未签名(alg=none)token 必须被拒绝。"""
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    unsigned = jwt.encode(claims, key="", algorithm="none")
    with pytest.raises(TokenRejected):
        verify_token(unsigned, secret=TEST_SECRET)


def test_token_carrying_role_claim_is_rejected() -> None:
    """架构文档 §2:角色/权限不写入 token;即使签名正确,多余 claim 也拒绝。"""
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": str(uuid.uuid4()),
        "role": "WORKSPACE_OWNER",
        "permissions": ["product.price.write"],
    }
    forged = jwt.encode(claims, TEST_SECRET, algorithm="HS256")
    with pytest.raises(TokenRejected):
        verify_token(forged, secret=TEST_SECRET)


def test_resolve_secret_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_JWT_SECRET", "env-secret")
    assert resolve_secret() == "env-secret"
    monkeypatch.delenv("RUNTIME_JWT_SECRET", raising=False)
    assert resolve_secret() == LOCAL_TEST_SECRET


# ---------------------------------------------------------------------------
# argon2 密码哈希
# ---------------------------------------------------------------------------


def test_password_hash_round_trip_and_wrong_password() -> None:
    digest = hash_password("correct-horse-battery")
    assert digest != "correct-horse-battery"
    assert verify_password(digest, "correct-horse-battery") is True
    assert verify_password(digest, "wrong-battery") is False


def test_password_hash_is_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_password_verify_rejects_malformed_hash() -> None:
    assert verify_password("not-an-argon2-hash", "any-password") is False
