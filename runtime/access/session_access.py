"""Session 可见性(计划 01 Task 5;架构文档 §5)。

首版 Session 为创建者私有:只有创建者能读取消息、提交后续输入或处理
Request;WorkspaceOwner 也不因角色自动获得会话内容。首版不定义共享或公开
可见性级别,因此除创建者外一律拒绝。
"""

from typing import Final
from uuid import UUID

from runtime.access.actor import ActorContext
from runtime.access.authorization import Decision


SESSION_PRIVATE: Final = "SESSION_PRIVATE"


class SessionAccess:
    """Session 读取可见性判定;首版仅创建者本人可读。"""

    def can_read(self, actor: ActorContext, *, created_by_actor_id: UUID) -> Decision:
        if actor.actor_id == created_by_actor_id:
            return Decision(allowed=True)
        return Decision(allowed=False, reason_code=SESSION_PRIVATE)
