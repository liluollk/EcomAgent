"""契约一致性测试 — 内置工具的 JSON 定义(_DEFINITIONS) 与 Python handler(_HANDLERS) 永不脱节。

背景：内置电商工具由两份手工维护的清单描述——
  - _DEFINITIONS：给大模型看的 function-calling JSON Schema（name/description/parameters）
  - _HANDLERS：真正执行的 Python 函数（**kwargs → str）

风险：改了函数签名（如加参数）但忘了改 JSON 定义 → 模型永远不知道新参数（静默脱节）；
或注册了一个 definition 却忘了挂 handler → 调用时 KeyError，执行时才暴露。

本测试在启动/CI 时双向校验，保证「JSON 契约」与「真实实现」永远对齐：
  1. definition 与 handler 名字集合双向一致（不多不少）。
  2. 每个 definition 的 required 参数都出现在 parameters.properties 里（Schema 自洽）。
  3. 每个 definition 都是合法 OpenAI function-calling 形状。
"""

from sources import builtin_tools


# 引擎注入字段白名单：这些字段由 agent_core 在权限检查前注入工具输入
# （如 operation_id），不出现在模型的 JSON Schema 里，因此不要求 schema 声明。
# 仅允许显式白名单内的字段——禁止「任意额外参数都行」，防止 handler 偷偷加参数
# 而不经模型知晓（那会破坏「schema 与 handler 签名对齐」的契约）。
ENGINE_INJECTED_FIELDS = {"operation_id"}


def test_definition_names_and_handler_names_are_identical():
    """内置工具给模型的 JSON 名集合，必须与实际 handler 名集合完全一致。"""
    def_names = {d["name"] for d in builtin_tools.get_definitions()}
    handler_names = set(builtin_tools.get_handlers())
    assert def_names == handler_names, (
        f"JSON 定义与 handler 脱节: 仅定义={def_names - handler_names}, "
        f"仅 handler={handler_names - def_names}"
    )


def test_every_required_param_has_schema():
    """每个 definition 声明的 required 参数，都必须能在 properties 里找到其类型描述。"""
    for d in builtin_tools.get_definitions():
        props = d["parameters"].get("properties", {})
        for req in d["parameters"].get("required", []):
            assert req in props, (
                f"工具 {d['name']} 声明 required 参数 '{req}'，但 JSON Schema 的 properties 里没有它"
            )


def test_every_property_is_typed():
    """properties 里每个参数都应声明 type（OpenAI function-calling 的基本形状）。"""
    for d in builtin_tools.get_definitions():
        for pname, pdef in d["parameters"].get("properties", {}).items():
            assert isinstance(pdef, dict) and "type" in pdef, (
                f"工具 {d['name']} 参数 '{pname}' 缺少 type 声明"
            )


def test_definitions_are_valid_function_calling_shape():
    """每条定义都具备 name + description + parameters(type=object)，可安全喂给模型后端。"""
    for d in builtin_tools.get_definitions():
        assert d.get("name"), "definition 缺 name"
        assert d.get("description"), f"工具 {d.get('name')} 缺 description"
        params = d.get("parameters")
        assert isinstance(params, dict) and params.get("type") == "object", (
            f"工具 {d.get('name')} 的 parameters 必须是 type=object 的 JSON Schema"
        )
        assert isinstance(params.get("properties"), dict)


def test_every_builtin_handler_has_explicit_tool_policy():
    """每个内置 handler 都必须声明平台安全策略。"""
    handler_names = set(builtin_tools.get_handlers())
    policy_names = set(builtin_tools.get_tool_policies())
    assert handler_names <= policy_names, (
        f"工具安全策略缺少 handler: {handler_names - policy_names}"
    )


def test_load_skill_has_explicit_read_policy():
    """元工具 load_skill 只加载上下文，不应触发写操作审批。"""
    assert builtin_tools.get_tool_policies()["load_skill"] == {
        "side_effect": "read",
        "requires_approval": False,
    }


def test_engine_injected_fields_are_whitelisted():
    """handler 签名里所有未在 schema 声明的参数，必须落在 ENGINE_INJECTED_FIELDS 白名单。

    引擎会向工具输入注入 operation_id 等字段，这些字段刻意不写进模型可见的
    JSON Schema；但除白名单外，handler 不得出现任何「模型不知道」的参数，否则
    即是 schema 与 handler 脱节的隐患（禁止放宽成「随便多参数都行」）。
    """
    import inspect

    for d in builtin_tools.get_definitions():
        handler = builtin_tools.get_handlers().get(d["name"])
        assert handler is not None, f"默认工具 {d['name']} 必须有对应 handler"
        props = set(d["parameters"].get("properties", {}))
        try:
            params = set(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            params = set()
        extra = params - props
        assert extra <= ENGINE_INJECTED_FIELDS, (
            f"工具 {d['name']} 的 handler 存在未在 schema 声明的参数 "
            f"{extra - ENGINE_INJECTED_FIELDS}（仅允许白名单 {ENGINE_INJECTED_FIELDS}）"
        )
    # operation_id 必须确为白名单成员，且是 update_price 的注入字段
    assert "operation_id" in ENGINE_INJECTED_FIELDS
    up = builtin_tools.get_handlers()["update_price"]
    assert "operation_id" in inspect.signature(up).parameters
