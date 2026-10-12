"""Skill 元数据、按需加载 Tool 与 Context 目录测试。"""

import importlib
import importlib.util
import uuid
from types import SimpleNamespace

import pytest

from agents.product_operations.agent import PRODUCT_AGENT_DEFINITION
from agents.product_operations.skill import PRODUCT_MAINTENANCE_SKILL
from composition.product_operations import _tool_definitions
from runtime.access.actor import ActorContext
from runtime.access.authorization import Authorizer
from runtime.capabilities.registry import (
    SkillDefinition,
    ToolBinding,
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.execution.pretool.pipeline import (
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    PassThroughInputNormalizer,
    PreToolProfileContext,
    runtime_control_registration,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interfaces.identifiers import ToolCallId
from runtime.interfaces.tools import ToolEffect
from runtime.orchestration import agent_loop, context
from runtime.persistence.base import MembershipRole


def test_skill_definition_keeps_a_model_visible_description() -> None:
    skill = SkillDefinition(
        name="商品维护",
        description="查询商品并按运营规则修改价格",
    )

    assert skill.description == "查询商品并按运营规则修改价格"


def test_context_exposes_skill_catalog_and_active_instruction_types() -> None:
    assert hasattr(context, "SkillCatalogEntry")
    assert hasattr(context, "ActiveSkillInstruction")


def test_load_skill_is_a_runtime_owned_read_tool() -> None:
    module_name = "runtime.capabilities.skill_activation"
    assert importlib.util.find_spec(module_name) is not None
    module = importlib.import_module(module_name)

    definition = module.LOAD_SKILL_DEFINITION
    assert definition.stable_key == "load_skill"
    assert definition.effect is ToolEffect.READ
    assert definition.required_permissions == frozenset()
    assert definition.input_json_schema["required"] == ["skill_key"]


def test_product_agent_declares_loadable_skill_metadata() -> None:
    module = importlib.import_module("runtime.capabilities.skill_activation")

    assert PRODUCT_MAINTENANCE_SKILL.description
    assert module.LOAD_SKILL_KEY in PRODUCT_AGENT_DEFINITION.required_tool_keys


def test_product_composition_registers_the_runtime_load_skill_tool() -> None:
    module = importlib.import_module("runtime.capabilities.skill_activation")
    definitions = dict(_tool_definitions())

    assert definitions[module.LOAD_SKILL_KEY] == module.LOAD_SKILL_DEFINITION


async def test_load_skill_skips_product_argument_normalization() -> None:
    module = importlib.import_module("runtime.capabilities.skill_activation")
    definition = module.LOAD_SKILL_DEFINITION
    registry = ToolRegistry()
    registry.register(
        ToolVersionRecord(
            stable_key=definition.stable_key,
            version_id=uuid.uuid4(),
            version=definition.version,
            workspace_id=None,
            source_kind="LOCAL",
            effect=definition.effect,
            input_schema=definition.input_json_schema,
            output_schema=definition.output_json_schema,
            schema_hash=definition.schema_hash,
            required_permissions=definition.required_permissions,
        )
    )
    # load_skill 是 Runtime 内置 Tool:绑定 Runtime 控制 Profile,
    # 不带任何商品业务扩展(归一化 / 来源 / 策略均为显式空实现)。
    registry.bind(
        ToolBinding(
            definition=definition,
            handler=module.LOAD_SKILL_HANDLER,
            pretool_registration=runtime_control_registration(),
        )
    )

    actor = ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=MembershipRole.WORKSPACE_OWNER,
        permissions=frozenset(),
        resource_scopes={},
        request_id="request-1",
    )
    request = ToolCallRequest(
        tool_call_id=ToolCallId(uuid.uuid4()),
        stable_key=module.LOAD_SKILL_KEY,
        version=definition.version,
        raw_arguments='{"skill_key":"product_maintenance"}',
    )
    dependencies = PreToolDependencies(
        tool_registry=RuntimeToolRegistry(registry),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
        profile_context=PreToolProfileContext(
            session=None, actor=actor, request=request
        ),
    )
    decision = await PreToolUsePipeline(
        actor=actor, stages=default_stages(dependencies)
    ).evaluate(request)

    assert decision.kind == "ALLOW"

    # 商品参数归一化不会介入内置 Tool:控制 Profile 的归一化器是透传实现。
    control_profile = await runtime_control_registration().factory.create(
        PreToolProfileContext(session=None, actor=actor, request=request)
    )
    assert isinstance(control_profile.input_normalizer, PassThroughInputNormalizer)


async def test_skill_activation_is_idempotent_and_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("runtime.capabilities.skill_activation")
    assert hasattr(module, "SkillActivationService")
    skill_version_id = uuid.uuid4()
    skill = SimpleNamespace(
        id=uuid.uuid4(),
        stable_key="product_maintenance",
        name="商品维护",
    )
    version = SimpleNamespace(
        id=skill_version_id,
        version=2,
        content_json={
            "description": "查询商品并执行维护",
            "prompt_fragments": {"instructions": "完整商品维护 SOP"},
        },
    )
    pinned_row = (SimpleNamespace(), version, skill)

    class Result:
        def all(self) -> list[tuple[object, object, object]]:
            return [pinned_row]

    class ScalarResult:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        def all(self) -> list[object]:
            return list(self._events)

    class Session:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def execute(self, _statement: object) -> Result:
            return Result()

        async def scalars(self, _statement: object) -> ScalarResult:
            return ScalarResult(self.events)

    session = Session()

    class Writer:
        def __init__(self, *, session: Session, workspace_id: uuid.UUID) -> None:
            self._session = session
            self._workspace_id = workspace_id

        async def append(
            self,
            run_id: uuid.UUID,
            event_kind: str,
            payload: dict[str, object],
        ) -> object:
            event = SimpleNamespace(
                run_id=run_id,
                event_type=event_kind,
                payload=payload,
                sequence_no=len(self._session.events) + 1,
            )
            self._session.events.append(event)
            return event

    monkeypatch.setattr(module, "AgentEventWriter", Writer, raising=False)
    service = module.SkillActivationService(session, workspace_id=uuid.uuid4())
    run_id = uuid.uuid4()

    first = await service.activate(run_id, "product_maintenance")
    second = await service.activate(run_id, "product_maintenance")
    restored = await service.active_for_run(run_id)

    assert first.already_active is False
    assert second.already_active is True
    assert first.instructions == "完整商品维护 SOP"
    assert len(session.events) == 1
    assert restored == (
        context.ActiveSkillInstruction(
            stable_key="product_maintenance",
            version=2,
            content="完整商品维护 SOP",
        ),
    )


async def test_agent_loop_rebuilds_active_skill_context_from_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = context.ActiveSkillInstruction(
        stable_key="product_maintenance",
        version=2,
        content="完整商品维护 SOP",
    )

    class Store:
        async def list_completed_messages(self, *, session_id: uuid.UUID) -> list[object]:
            return []

    class ActivationService:
        def __init__(self, _session: object, *, workspace_id: uuid.UUID) -> None:
            pass

        async def active_for_run(
            self, _run_id: uuid.UUID
        ) -> tuple[context.ActiveSkillInstruction, ...]:
            return (expected,)

    monkeypatch.setattr(
        agent_loop,
        "SkillActivationService",
        ActivationService,
        raising=False,
    )
    loop = agent_loop.AgentLoop.__new__(agent_loop.AgentLoop)
    loop._store = Store()
    loop._session = object()
    loop._workspace_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), session_id=uuid.uuid4())

    view = await loop._conversation_view(run)

    assert view.active_skill_instructions == (expected,)


async def test_agent_loop_executes_load_skill_inside_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("runtime.capabilities.skill_activation")
    activated = module.ActivatedSkill(
        skill_version_id=uuid.uuid4(),
        stable_key="product_maintenance",
        version=2,
        instructions="完整商品维护 SOP",
        already_active=False,
    )

    class ActivationService:
        def __init__(self, _session: object, *, workspace_id: uuid.UUID) -> None:
            pass

        async def activate(self, _run_id: uuid.UUID, skill_key: str) -> object:
            assert skill_key == "product_maintenance"
            return activated

    monkeypatch.setattr(agent_loop, "SkillActivationService", ActivationService)
    loop = agent_loop.AgentLoop.__new__(agent_loop.AgentLoop)
    loop._session = object()
    loop._workspace_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4())
    call = SimpleNamespace(id=uuid.uuid4())

    receipt = await loop._load_skill_receipt(
        run,
        call,
        {"skill_key": "product_maintenance"},
    )

    assert receipt.status == "SUCCEEDED"
    assert receipt.payload == activated.tool_payload()
