"""持久化 ORM 模型集合。

导入全部模型模块,确保 ``Base.metadata`` 覆盖架构文档中的每一张表;
Alembic env.py 与迁移生成依赖该聚合导入。
"""

from runtime.persistence.models import (  # noqa: F401
    configuration,
    events,
    execution,
    identity,
    integrations,
    interaction,
)
from runtime.persistence.models.configuration import (  # noqa: F401
    Agent,
    AgentVersion,
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
    Source,
    SourceVersion,
    ToolDefinition,
    ToolVersion,
    WorkspaceAgentBinding,
    WorkspaceSkillBinding,
    WorkspaceSourceBinding,
    WorkspaceToolBinding,
)
from runtime.persistence.models.events import AgentEvent, AuditEvent  # noqa: F401
from runtime.persistence.models.execution import (  # noqa: F401
    Operation,
    OperationAttempt,
    Run,
    RunAttempt,
    RunJob,
    RunPolicySnapshot,
    RunSkillVersion,
    RunSourceVersion,
    RunToolVersion,
    ToolCall,
)
from runtime.persistence.models.identity import (  # noqa: F401
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)
from runtime.persistence.models.integrations import (  # noqa: F401
    IntegrationOutbox,
    MemoryRecord,
    Secret,
)
from runtime.persistence.models.interaction import (  # noqa: F401
    ConfirmationRequest,
    InputRequest,
    Message,
    Session,
)
