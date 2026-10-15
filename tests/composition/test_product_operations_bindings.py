"""ProductOperations 声明式 ToolBinding 组装测试。"""

from __future__ import annotations

import uuid

from composition.product_operations_bindings import flatten_versioned_bindings
from runtime.capabilities.registry import ToolBinding, VersionedToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.profiles import runtime_control_registration
from runtime.interfaces.tools import ToolDefinition, ToolEffect


def _versioned(key: str, *, make_current: bool) -> VersionedToolBinding:
    input_schema = {"type": "object"}
    output_schema = {"type": "object"}
    definition = ToolDefinition(
        stable_key=key,
        version=1,
        description=key,
        input_json_schema=input_schema,
        output_json_schema=output_schema,
        effect=ToolEffect.READ,
        required_permissions=frozenset(),
        source_kind="LOCAL",
        schema_hash=compute_schema_hash(input_schema, output_schema),
    )
    return VersionedToolBinding(
        version_id=uuid.uuid4(),
        binding=ToolBinding(
            definition=definition,
            handler=object(),
            pretool_registration=runtime_control_registration(),
        ),
        make_current=make_current,
    )


def test_flatten_versioned_bindings_preserves_groups_and_current_flags() -> None:
    first = _versioned("search_products", make_current=True)
    legacy = _versioned("get_product_snapshot", make_current=False)

    assert flatten_versioned_bindings(((first,), (legacy,))) == (first, legacy)


def test_flatten_versioned_bindings_rejects_duplicate_version_id() -> None:
    first = _versioned("search_products", make_current=True)
    duplicate = VersionedToolBinding(
        version_id=first.version_id,
        binding=_versioned("get_listing", make_current=True).binding,
        make_current=True,
    )

    try:
        flatten_versioned_bindings(((first,), (duplicate,)))
    except ValueError as error:
        assert "duplicate ToolVersion ID" in str(error)
    else:  # pragma: no cover - 明确锁定 fail closed
        raise AssertionError("duplicate ToolVersion ID was accepted")
