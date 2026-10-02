"""通用资源授权判定(计划 01 Task 5;架构文档 §4)。

授权结果由三个维度共同决定:成员角色 ∩ 资源数据范围 ∩ 动作权限。

- 动作权限:``required_permissions`` 必须全部包含于 ActorContext.permissions;
- 资源范围:每个 ``(resource_type, resource_id)`` 必须落在对应类型的
  ResourceScope 内——``mode=ALL`` 放行任意 id;``SELECTED`` 要求 id 在集合内;
  类型无 scope 映射一律拒绝;
- 只读调用不传资源引用,仅做动作权限校验。
"""

from typing import Final

from pydantic import BaseModel, ConfigDict

from runtime.access.actor import ActorContext


PERMISSION_DENIED: Final = "PERMISSION_DENIED"
RESOURCE_SCOPE_DENIED: Final = "RESOURCE_SCOPE_DENIED"


class Decision(BaseModel):
    """授权判定结果;``allowed=False`` 时 ``reason_code`` 说明原因。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str | None = None


class Authorizer:
    """无状态授权器;判定依据全部来自 ActorContext。"""

    def authorize(
        self,
        actor: ActorContext,
        *,
        required_permissions: frozenset[str],
        resources: frozenset[tuple[str, str]],
    ) -> Decision:
        if not required_permissions <= actor.permissions:
            return Decision(allowed=False, reason_code=PERMISSION_DENIED)
        for resource_type, resource_id in resources:
            scope = actor.resource_scopes.get(resource_type)
            if scope is None:
                return Decision(allowed=False, reason_code=RESOURCE_SCOPE_DENIED)
            if scope.mode == "SELECTED" and resource_id not in scope.resource_ids:
                return Decision(allowed=False, reason_code=RESOURCE_SCOPE_DENIED)
        return Decision(allowed=True)
