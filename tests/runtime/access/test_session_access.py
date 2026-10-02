"""Session 可见性测试(计划 01 Task 5 Step 1;架构文档 §5)。

架构文档首版语义:Session 为创建者私有,只有创建者能读取消息、提交输入或
处理 Request;WorkspaceOwner 也不因角色自动获得会话内容(只能通过审计界面
查看脱敏记录)。首版不定义共享/公开可见性级别。
"""


async def test_creator_can_read_own_session(session_access, owner) -> None:
    decision = session_access.can_read(owner, created_by_actor_id=owner.actor_id)
    assert decision.allowed is True
    assert decision.reason_code is None


async def test_owner_cannot_read_another_members_private_session(
    session_access, owner, identity_seed
) -> None:
    """计划原文:owner 不能读取 operator 创建的私有 Session。"""
    decision = session_access.can_read(
        owner, created_by_actor_id=identity_seed.operator_user.id
    )
    assert decision.allowed is False
    assert decision.reason_code == "SESSION_PRIVATE"


async def test_other_member_cannot_read_private_session(
    session_access, operator, identity_seed
) -> None:
    """对称用例:operator 也不能读取 owner 的私有 Session。"""
    decision = session_access.can_read(
        operator, created_by_actor_id=identity_seed.owner_user.id
    )
    assert decision.allowed is False
    assert decision.reason_code == "SESSION_PRIVATE"
