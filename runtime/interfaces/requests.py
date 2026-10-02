"""Request 公共类型(架构文档 §9)。

架构文档在 PreToolUse 统一决策中定义了 REQUIRE_INPUT(InputRequirement) 与
REQUIRE_CONFIRMATION(ConfirmationSpec);两者的字段结构由交互/请求阶段定义,
此处只固定文档已写明的类型名与不可变约束,不提前发明字段。
"""

from pydantic import BaseModel, ConfigDict


class InputRequirement(BaseModel):
    """REQUIRE_INPUT 决策携带的用户输入需求(架构文档 §9)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmationSpec(BaseModel):
    """REQUIRE_CONFIRMATION 决策携带的确认规格(架构文档 §9)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
