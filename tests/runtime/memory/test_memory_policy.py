"""记忆入库策略测试(计划 04 Task 7 Step 1)。

执行关键事实(权限、资源范围、业务规则、凭据)禁止进入长期记忆;
Runtime 只保存已确认的偏好/反馈/决策/经验,Secret/凭据形态的内容一律拒绝。
执行关键事实的分类名称领域无关:业务规则统一归入 ``business_rule``,
``price_rule`` 等历史商品名称按未知 kind 继续拒绝。
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.models.integrations import MemoryRecord


# 执行关键事实禁止入库(通用分类;商品专属名称不再出现在 Runtime 源码)。
EXECUTION_CRITICAL_KINDS = ["permission", "resource_scope", "business_rule", "credential"]

# Runtime 只保存已确认的记忆类型(架构文档 §7:过滤后的长期记忆)。
ACCEPTED_KINDS = ["preference", "feedback", "decision", "experience"]


@pytest.mark.parametrize("kind", EXECUTION_CRITICAL_KINDS)
def test_execution_critical_facts_are_not_memories(
    memory_service, memory_candidate, kind: str
) -> None:
    assert memory_service.accept(memory_candidate(kind=kind)) is False


def test_legacy_price_rule_is_still_rejected_as_unknown(memory_service, memory_candidate) -> None:
    """旧商品名称不再是执行关键事实分类,但作为未知 kind 仍然 fail-closed。"""
    assert memory_service.accept(memory_candidate(kind="price_rule")) is False


@pytest.mark.parametrize("kind", ACCEPTED_KINDS)
async def test_accepted_kinds_are_persisted_as_active(
    memory_service,
    db_session: AsyncSession,
    seeded_workspace,
    memory_candidate,
    kind: str,
) -> None:
    accepted = memory_service.accept(
        memory_candidate(kind=kind, subject_key="pref-replies")
    )
    assert accepted is True
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace.workspace.id,
                MemoryRecord.subject_key == "pref-replies",
            )
        )
    ).one()
    assert record.lifecycle_status == "ACTIVE"
    assert record.kind == kind
    assert record.revision == 1
    assert record.content == "prefer short replies"


async def test_rejected_candidates_leave_no_record(
    memory_service,
    db_session: AsyncSession,
    seeded_workspace,
    memory_candidate,
) -> None:
    memory_service.accept(memory_candidate(kind="credential"))
    count = await db_session.scalar(
        select(func.count())
        .select_from(MemoryRecord)
        .where(MemoryRecord.workspace_id == seeded_workspace.workspace.id)
    )
    assert count == 0


def test_secret_like_content_is_rejected_even_as_preference(
    memory_service, memory_candidate
) -> None:
    candidate = memory_candidate(content="my api key is sk-abcdef0123456789abcdef")
    assert memory_service.accept(candidate) is False


def test_accept_rejects_unknown_kinds(memory_service, memory_candidate) -> None:
    assert memory_service.accept(memory_candidate(kind="system_instruction")) is False


def test_candidate_requires_known_fields(memory_candidate) -> None:
    candidate = memory_candidate(subject_key=f"subject-{uuid.uuid4()}")
    assert candidate.subject_key.startswith("subject-")
    assert candidate.content == "prefer short replies"
