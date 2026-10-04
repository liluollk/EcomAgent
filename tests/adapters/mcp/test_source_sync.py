"""MCP Source 同步测试(计划 04 Task 6 Step 1)。

覆盖:Schema 版本化(变化创建新版本,不覆盖旧版本)、未分类 Tool 默认
禁用、MCP Server 声明的 effect/permission 被忽略、本地分类绑定后启用、
重复同步幂等、工具字段保存正确。
"""

from adapters.mcp.client import McpToolDescriptor
from adapters.mcp.source_sync import LOCAL_CLASSIFICATION_REQUIRED
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.tools import ToolEffect


def schema_version_one() -> McpToolDescriptor:
    return McpToolDescriptor(
        name="weknora_search",
        description="检索 WeKnora 知识库",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


def schema_version_two() -> McpToolDescriptor:
    return McpToolDescriptor(
        name="weknora_search",
        description="检索 WeKnora 知识库",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    )


async def test_new_mcp_schema_requires_local_reclassification(source_sync) -> None:
    first = await source_sync.sync(schema_version_one())
    second = await source_sync.sync(schema_version_two())
    assert second.tool_version_id != first.tool_version_id
    assert second.enabled is False
    assert second.disabled_reason == "LOCAL_CLASSIFICATION_REQUIRED"


async def test_duplicate_sync_is_idempotent(source_sync) -> None:
    first = await source_sync.sync(schema_version_one())
    again = await source_sync.sync(schema_version_one())
    assert again.tool_version_id == first.tool_version_id
    assert again.version == first.version
    assert again.schema_hash == first.schema_hash


async def test_schema_change_creates_new_version_and_keeps_old(
    source_sync, tool_store
) -> None:
    first = await source_sync.sync(schema_version_one())
    second = await source_sync.sync(schema_version_two())

    old = tool_store.get_version(first.tool_version_id)
    assert old is not None
    assert old.schema_hash == first.schema_hash
    assert old.input_schema == first.input_schema
    assert old.version == 1
    assert second.version == 2
    assert second.enabled is False


async def test_description_change_also_requires_new_classification(source_sync) -> None:
    first = await source_sync.sync(schema_version_one())
    renamed = schema_version_one().model_copy(update={"description": "更新后的描述"})
    second = await source_sync.sync(renamed)
    assert second.tool_version_id != first.tool_version_id
    assert second.enabled is False
    assert second.disabled_reason == LOCAL_CLASSIFICATION_REQUIRED


async def test_server_declared_effect_and_permissions_are_ignored(source_sync) -> None:
    declared = McpToolDescriptor(
        name="delete_listing",
        description="MCP Server 自行声明为 WRITE 的工具",
        input_schema={"type": "object"},
        declared_effect="WRITE",
        declared_permissions=frozenset({"listing.write"}),
    )
    record = await source_sync.sync(declared)
    assert record.effect is None
    assert record.required_permissions == frozenset()
    assert record.enabled is False
    assert record.disabled_reason == LOCAL_CLASSIFICATION_REQUIRED


async def test_local_classification_binds_effect_and_enables_tool(
    source_sync, tool_store
) -> None:
    record = await source_sync.sync(schema_version_one())
    assert record.enabled is False

    classified = await source_sync.classify(
        record.tool_version_id,
        effect=ToolEffect.READ,
        required_permissions={"knowledge.read"},
    )
    assert classified.enabled is True
    assert classified.disabled_reason is None
    assert classified.effect is ToolEffect.READ
    assert classified.required_permissions == frozenset({"knowledge.read"})

    stored = tool_store.get_version(record.tool_version_id)
    assert stored is not None
    assert stored.enabled is True


async def test_resync_after_classification_keeps_enabled_version(source_sync) -> None:
    record = await source_sync.sync(schema_version_one())
    await source_sync.classify(record.tool_version_id, effect=ToolEffect.READ)

    again = await source_sync.sync(schema_version_one())
    assert again.tool_version_id == record.tool_version_id
    assert again.enabled is True
    assert again.effect is ToolEffect.READ


async def test_synced_tool_fields_are_saved(source_sync) -> None:
    descriptor = schema_version_one()
    record = await source_sync.sync(descriptor)
    assert record.name == descriptor.name
    assert record.description == descriptor.description
    assert record.input_schema == descriptor.input_schema
    assert record.output_schema == {}
    assert record.source_kind == "MCP"
    assert record.version == 1
    assert record.stable_key == f"mcp.{descriptor.name}"
    assert record.schema_hash == compute_schema_hash(
        descriptor.input_schema, descriptor.output_schema
    )
