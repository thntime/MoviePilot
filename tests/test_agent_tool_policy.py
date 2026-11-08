"""
验证内置工具策略覆盖、只读例外和敏感操作权限。
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, Field

import app.agent.orchestrator as agent_module
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.summarization import FinalRequestCompactionMiddleware

# 策略包根通过 __getattr__ 惰性导出，Pylint 无法静态解析。
# pylint: disable=no-name-in-module
from app.agent.policy import (
    DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    DEFAULT_TOOL_POLICY_REGISTRY,
    ActionEffect,
    AuthSource,
    ConfirmationMode,
    ExecutionOutcome,
    MigrationState,
    PolicyPrincipal,
    PrincipalRole,
    PrincipalType,
    RecoveryMode,
    ResultSensitivity,
    ToolOrigin,
    ToolPolicyContext,
    call_policy_hook,
)
from app.agent.policy.mcp import OPERATION_DESCRIPTIONS
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.ask_user_choice import AskUserChoiceTool
from app.agent.tools.impl.send_local_file import SendLocalFileTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.schemas.types import NotificationChannel


class _EchoInput(BaseModel):
    """
    策略测试工具的输入契约。
    """

    query: str = Field(description="待回显文本")


class _AgentFactoryLLM:
    """
    满足 Agent 构建与摘要中间件所需合同的测试模型。
    """

    _llm_type = "openai-chat"
    model = "fake"
    profile = {"max_input_tokens": 64000}

    def with_retry(self):
        """
        返回可供摘要中间件调用的 Runnable。
        """
        return self


class _EchoTool(MoviePilotTool):
    """
    返回输入文本的测试工具。
    """

    name: str = "policy_echo"
    description: str = "Echo policy test input."
    args_schema: type[BaseModel] = _EchoInput

    async def run(self, query: str) -> str:
        """
        返回输入文本。
        """
        return query


class _OverrideArunTool(_EchoTool):
    """
    模拟插件覆盖宿主工具基类 `_arun` 的实现。
    """

    name: str = "plugin_override_arun"

    def __init__(self, events: list[str], **kwargs):
        super().__init__(**kwargs)
        self._events = events

    async def _arun(self, *args, **kwargs) -> str:
        """
        绕过基类实现并记录真实执行顺序。
        """
        self._events.append("tool")
        return str(kwargs.get("query") or "ok")


class _FailingTool(_EchoTool):
    """
    抛出固定异常以验证 direct manager 的 observation fail-open。
    """

    name: str = "policy_failure"

    async def run(self, query: str) -> str:
        """
        模拟真实工具失败。
        """
        raise ValueError(f"tool-error:{query}")


class _AdminSafeReadTool(_EchoTool):
    """
    声明管理员门禁的动态测试工具。
    """

    name: str = "admin_test_tool"
    require_admin: bool = True

    def __init__(self, events: list[str], **kwargs):
        super().__init__(**kwargs)
        self._events = events

    async def run(self, query: str) -> str:
        """
        记录实际执行，区分旧门禁放行与拒绝。
        """
        self._events.append("run")
        return query


def _tool_class_name(tool_class: type[MoviePilotTool]) -> str:
    """
    从 Pydantic 字段默认值读取工具类的稳定名称。
    """
    return str(tool_class.model_fields["name"].default)


def _interactive_context(*, is_admin: bool = True) -> ToolPolicyContext:
    """
    构造会随本轮管理员上下文刷新的交互式策略上下文。
    """
    return ToolPolicyContext(
        session_id="session-1",
        user_id="user-1",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.CHANNEL,
        channel="telegram",
        source="user",
        agent_context={"is_admin": is_admin},
    )


def test_builtin_policy_inventory_covers_every_fixed_tool() -> None:
    """
    固定内置工具 inventory 必须随工厂入口同步。
    """
    fixed_tool_names = {_tool_class_name(tool_class) for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES}
    fixed_tool_names.update(
        {
            _tool_class_name(AskUserChoiceTool),
            _tool_class_name(SendLocalFileTool),
            _tool_class_name(SendVoiceMessageTool),
            _tool_class_name(MoviePilotApiTool),
        }
    )
    fixed_tool_names.update(
        _tool_class_name(tool_class)
        for tool_class in MoviePilotToolFactory.EXTERNAL_SERVICE_TOOL_CLASSES
    )

    assert DEFAULT_TOOL_POLICY_REGISTRY.builtin_tool_inventory == fixed_tool_names


def test_registry_applies_safe_read_exceptions_and_defaults_to_shadow() -> None:
    """
    SAFE_READ 仅用于明确例外，其他固定和动态工具默认 shadow。
    """
    safe_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="persona",
        arguments={"action": "list"},
        requires_admin=False,
    )
    persona_switch_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="persona",
        arguments={"action": "switch", "persona_id": "concise"},
        requires_admin=False,
    )
    admin_safe_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={"operation_id": "scheduler.list"},
        requires_admin=True,
    )
    shadow_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="update_system_settings",
        arguments={"updates": []},
        requires_admin=True,
    )
    dynamic_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="plugin_unknown_tool",
        arguments={"action": "custom"},
        requires_admin=False,
    )

    assert safe_policy.effect is ActionEffect.SAFE_READ
    assert safe_policy.result_sensitivity is ResultSensitivity.NORMAL
    assert safe_policy.migration_state is MigrationState.ENFORCED
    assert persona_switch_policy.required_role is PrincipalRole.SYSTEM_ADMIN

    assert admin_safe_policy.effect is ActionEffect.SAFE_READ
    assert admin_safe_policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert admin_safe_policy.migration_state is MigrationState.ENFORCED

    assert shadow_policy.migration_state is MigrationState.LEGACY_SHADOW
    assert shadow_policy.effect is ActionEffect.UNKNOWN
    assert shadow_policy.required_role is PrincipalRole.SYSTEM_ADMIN

    assert dynamic_policy.migration_state is MigrationState.LEGACY_SHADOW
    assert dynamic_policy.effect is ActionEffect.UNKNOWN
    assert dynamic_policy.result_sensitivity is ResultSensitivity.UNKNOWN


@pytest.mark.parametrize("show_secrets", [None, False])
def test_system_settings_without_secret_values_is_enforced_read(
    show_secrets,
) -> None:
    """
    普通设置读取维持既有兼容路径，不增加确认。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={
            "operation_id": "config.system.get",
            "body": {"show_secrets": show_secrets},
        },
        requires_admin=True,
    )

    assert policy.migration_state is MigrationState.ENFORCED
    assert policy.effect is ActionEffect.SAFE_READ


def test_system_settings_secret_read_has_enforced_sensitive_policy() -> None:
    """
    显式读取密钥只能进入宿主强制确认策略。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={
            "operation_id": "config.system.get",
            "body": {"show_secrets": True},
        },
        requires_admin=True,
    )

    assert policy.migration_state is MigrationState.ENFORCED
    assert policy.effect is ActionEffect.SENSITIVE_READ
    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert policy.confirmation.value == "required"
    assert policy.result_sensitivity is ResultSensitivity.SECRET
    assert policy.background_allowed is False
    assert policy.subagent_allowed is False


def test_database_operation_secret_read_has_enforced_sensitive_policy() -> None:
    """
    显式读取凭据配置表只能进入宿主强制确认策略，结果不得写入回执摘要。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="database_operation",
        arguments={
            "action": "query",
            "arguments": {
                "sql": "SELECT value FROM systemconfig",
                "show_secrets": True,
            },
        },
        requires_admin=True,
    )

    assert policy.effect is ActionEffect.SENSITIVE_READ
    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert policy.confirmation.value == "required"
    assert policy.result_sensitivity is ResultSensitivity.SECRET
    assert policy.migration_state is MigrationState.ENFORCED
    assert policy.background_allowed is False
    assert policy.subagent_allowed is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"sql": "SELECT 1"},
        {"sql": "SELECT 1", "show_secrets": False},
        {"sql": "SELECT 1", "show_secrets": None},
        {"sql": "SELECT 1", "show_secrets": "true"},
    ],
    ids=["plain", "false", "null", "string"],
)
def test_database_operation_without_secret_read_keeps_legacy_shadow(arguments) -> None:
    """
    未显式请求凭据表读取时数据库工具维持既有 shadow 观测，非布尔取值也不升级。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="database_operation",
        arguments={"action": "query", "arguments": arguments},
        requires_admin=True,
    )

    assert policy.migration_state is MigrationState.LEGACY_SHADOW
    assert policy.effect is ActionEffect.UNKNOWN
    assert policy.result_sensitivity is ResultSensitivity.UNKNOWN


def test_database_operation_secret_write_stays_unclassified() -> None:
    """
    凭据表写入保持写入类工具的既有策略，不按敏感读取处理。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="database_operation",
        arguments={
            "action": "write",
            "arguments": {
                "sql": "UPDATE systemconfig SET value = 'x' WHERE id = 1",
                "show_secrets": True,
            },
        },
        requires_admin=True,
    )

    assert policy.effect is ActionEffect.UNKNOWN
    assert policy.migration_state is MigrationState.LEGACY_SHADOW


def test_system_settings_secret_read_query_cannot_bypass_confirmation() -> None:
    """
    GET 查询参数不能绕过敏感设置读取的确认策略。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={
            "operation_id": "config.system.get",
            "query": {"show_secrets": True},
        },
        requires_admin=True,
    )

    assert policy.effect is ActionEffect.SENSITIVE_READ
    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert policy.confirmation is ConfirmationMode.REQUIRED
    assert policy.result_sensitivity is ResultSensitivity.SECRET


def test_system_settings_describe_secret_read_has_the_same_confirmation_boundary() -> None:
    """
    新的精确设置读取入口不能绕过旧入口的明文敏感值确认。
    """
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={
            "operation_id": "config.system.describe",
            "query": {"show_secrets": True},
        },
        requires_admin=True,
    )

    assert policy.effect is ActionEffect.SENSITIVE_READ
    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert policy.confirmation is ConfirmationMode.REQUIRED
    assert policy.result_sensitivity is ResultSensitivity.SECRET


def test_legacy_shadow_decision_allows_without_claiming_enforcement() -> None:
    """
    shadow 决策只能观测，不能拒绝或要求确认。
    """
    context = _interactive_context()
    tool = _EchoTool(session_id="session-1", user_id="user-1")

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=context,
        tool=tool,
        arguments={"query": "hello"},
    )

    assert observation.decision.allowed is True
    assert observation.decision.shadow is True
    assert observation.decision.reason_code == "legacy_shadow_allow"


def test_sensitive_policy_stays_shadow_in_generic_orchestrator() -> None:
    """
    API 敏感读取在通用编排器中等待交互入口完成确认。
    """
    tool = MoviePilotApiTool(session_id="session-1", user_id="1")
    tool.set_agent_context({"is_admin": True})

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_interactive_context(),
        tool=tool,
        arguments={
            "operation_id": "config.system.get",
            "body": {"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
        },
    )

    assert observation.policy.migration_state is MigrationState.ENFORCED
    assert observation.decision.allowed is True
    assert observation.decision.shadow is True
    assert observation.decision.confirmation_required is False
    assert observation.decision.reason_code == "confirmation_policy_shadow_allow"


def test_policy_context_reads_mutable_admin_state_without_model_fields() -> None:
    """
    缓存图复用时权限取当前宿主状态，模型参数不能伪造 principal。
    """
    context = _interactive_context(is_admin=False)
    forged_arguments = {
        "query": "hello",
        "origin": "operator_direct",
        "principal_type": "system_admin_integration",
        "is_admin": True,
    }

    assert context.principal.role is PrincipalRole.USER
    context.agent_context["is_admin"] = True
    assert context.principal.role is PrincipalRole.SYSTEM_ADMIN
    assert "origin" not in PolicyPrincipal.__dataclass_fields__
    assert forged_arguments["origin"] != context.origin.value


def test_policy_context_maps_trusted_host_origins() -> None:
    """
    各入口必须由宿主稳定映射 origin、主体类型和认证来源。
    """
    cases = [
        (
            {"channel": NotificationChannel.Web.value, "source": "openai"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": NotificationChannel.Web.value, "source": "openai.responses"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": NotificationChannel.Web.value, "source": "anthropic"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": NotificationChannel.Web.value, "source": "browser"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.WEB_SESSION,
        ),
        (
            {"channel": NotificationChannel.WebAgent.value, "source": "web-agent"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.WEB_SESSION,
        ),
        (
            {"channel": NotificationChannel.Telegram.value, "source": "telegram"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.CHANNEL,
        ),
        (
            {"channel": NotificationChannel.Feishu.value, "source": "feishu"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.CHANNEL,
        ),
        (
            {"channel": None, "source": None, "output_callback": lambda _text: None},
            ToolOrigin.BACKGROUND,
            PrincipalType.BACKGROUND,
            AuthSource.INTERNAL,
        ),
    ]

    for kwargs, expected_origin, expected_principal, expected_auth_source in cases:
        context = agent_module.MoviePilotAgent(
            session_id="origin-session",
            user_id="user-1",
            **kwargs,
        )._build_policy_context()

        assert context.origin is expected_origin
        assert context.principal_type is expected_principal
        assert context.auth_source is expected_auth_source

    subagent_context = (
        agent_module.MoviePilotAgent(
            session_id="subagent-session",
            user_id="user-1",
            channel=NotificationChannel.Telegram.value,
            source="telegram",
        )
        ._build_policy_context()
        .for_subagent()
    )
    assert subagent_context.origin is ToolOrigin.SUBAGENT
    assert subagent_context.principal_type is PrincipalType.SUBAGENT
    assert subagent_context.auth_source is AuthSource.INTERNAL


def test_host_middleware_observes_plugin_before_overridden_arun() -> None:
    """
    插件覆盖 `_arun` 时，宿主 middleware 仍必须先执行策略。
    """
    events: list[str] = []
    tool = _OverrideArunTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-1",
            "name": tool.name,
            "args": {"query": "ok"},
        },
    )
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start

    def _record_start(**kwargs):
        events.append("policy")
        return original_start(**kwargs)

    async def _handler(_request):
        result = await tool._arun(query="ok")
        return ToolMessage(content=result, tool_call_id="call-1")

    with patch.object(
        DEFAULT_TOOL_POLICY_ORCHESTRATOR,
        "start",
        side_effect=_record_start,
    ):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert result.content == "ok"
    assert events == ["policy", "tool"]


@pytest.mark.parametrize("failed_phase", ["start", "finish"])
def test_middleware_observation_failure_does_not_replace_success(
    failed_phase: str,
) -> None:
    """
    shadow start/finish 故障不能阻止 handler 或替换成功结果。
    """
    orchestrator = MagicMock()
    orchestrator.start.return_value = SimpleNamespace(decision=SimpleNamespace(allowed=True))
    getattr(orchestrator, failed_phase).side_effect = RuntimeError(f"policy-{failed_phase}-failure")
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    request = SimpleNamespace(
        tool=_EchoTool(session_id="session-1", user_id="user-1"),
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )
    expected = ToolMessage(content="same", tool_call_id="call-1")
    handler_called = False

    async def _handler(_request):
        nonlocal handler_called
        handler_called = True
        return expected

    result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert handler_called is True
    assert result is expected


def test_middleware_fail_observation_does_not_mask_tool_error() -> None:
    """
    shadow fail hook 故障后仍返回真实工具故障，模型可以继续处理。
    """
    orchestrator = MagicMock()
    orchestrator.start.return_value = SimpleNamespace(decision=SimpleNamespace(allowed=True))
    orchestrator.fail.side_effect = RuntimeError("policy-fail-hook-failure")
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    request = SimpleNamespace(
        tool=_EchoTool(session_id="session-1", user_id="user-1"),
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )
    tool_error = ValueError("original-tool-failure")

    async def _handler(_request):
        raise tool_error

    result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert result.status == "error"
    assert "ValueError" in result.content
    assert "original-tool-failure" not in result.content
    assert "policy-fail-hook-failure" not in result.content


def test_middleware_keeps_shadow_observation_without_enforcing_decision() -> None:
    """
    普通 ToolNode 不得因 shadow 观测决策改变既有行为。
    """
    orchestrator = MagicMock()
    orchestrator.start.return_value = SimpleNamespace(decision=SimpleNamespace(allowed=False))
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    request = SimpleNamespace(
        tool=_EchoTool(session_id="session-1", user_id="user-1"),
        tool_call={"id": "call-1", "name": "policy_echo", "args": {"query": "same"}},
    )
    handler = AsyncMock(return_value=ToolMessage(content="same", tool_call_id="call-1"))

    result = asyncio.run(middleware.awrap_tool_call(request, handler))

    assert result.content == "same"
    handler.assert_awaited_once_with(request)
    orchestrator.finish.assert_called_once()
    orchestrator.fail.assert_not_called()


def test_enforced_policy_start_failure_blocks_handler() -> None:
    """
    强制策略无法形成决定时不得继续执行受保护工具。
    """
    orchestrator = MagicMock()
    orchestrator.start.side_effect = RuntimeError("policy-start-failure")
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    tool = _EchoTool(session_id="session-1", user_id="user-1")
    handler = AsyncMock(return_value="secret-result")

    executed, result = asyncio.run(
        middleware.execute_tool_call(
            tool=tool,
            arguments={"query": "secret"},
            invocation_id="strict-call-1",
            handler=handler,
        )
    )

    assert executed is False
    assert result == "宿主策略暂时不可用，未执行该工具。"
    handler.assert_not_awaited()


def test_policy_hook_failure_logs_only_stable_type_information() -> None:
    """
    fail-open 诊断只记录阶段和异常类型，不读取可能含凭据的异常文本。
    """
    mock_logger = MagicMock()

    def _fail() -> None:
        raise RuntimeError("DATABASE_PASSWORD=policy-secret-marker")

    with patch("app.agent.policy.orchestrator.logger", mock_logger):
        result = call_policy_hook("start", _fail)

    logged = "\n".join(str(call) for call in mock_logger.warning.call_args_list)
    assert result is None
    assert "phase=start" in logged
    assert "RuntimeError" in logged
    assert "policy-secret-marker" not in logged


def test_policy_hook_hostile_error_type_is_fail_open() -> None:
    """
    异常类型名协议不可信时，观测故障仍不得逃出 fail-open 边界。
    """
    secret_marker = "hostile-policy-type-secret-6274"

    class _HostileMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError(f"DATABASE_PASSWORD={secret_marker}")
            return super().__getattribute__(name)

    class _HostilePolicyError(RuntimeError, metaclass=_HostileMeta):
        pass

    mock_logger = MagicMock()

    def _fail() -> None:
        raise _HostilePolicyError("visible policy failure")

    escaped = False
    with patch("app.agent.policy.orchestrator.logger", mock_logger):
        try:
            result = call_policy_hook("start", _fail)
        except BaseException:
            escaped = True
            result = None

    logged = "\n".join(str(call) for call in mock_logger.warning.call_args_list)
    assert escaped is False
    assert result is None
    assert "phase=start" in logged
    assert secret_marker not in logged


@pytest.mark.parametrize(
    ("legacy_admin", "expected_result", "expected_run_count"),
    [
        (True, "same", 1),
        (
            False,
            (
                "抱歉，您没有执行此工具的权限。"
                "只有渠道管理员或系统管理员才能执行工具操作。"
                "如需执行工具，请联系管理员将您的用户ID添加到渠道管理员列表中（设定 -> 通知 -> 对应渠道配置 -> 管理员名单），"
                "或联系系统管理员为您设置管理员权限。"
            ),
            0,
        ),
    ],
)
def test_agent_admin_dynamic_tool_keeps_existing_authorization_authority(
    legacy_admin: bool,
    expected_result: str,
    expected_run_count: int,
) -> None:
    """
    动态工具仍由既有管理员门禁决定是否进入 handler。
    """
    events: list[str] = []
    observations = []
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start
    tool = _AdminSafeReadTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    tool.set_message_attr(
        channel=NotificationChannel.Telegram.value,
        source="user",
        username="member",
    )
    tool.set_agent_context({"is_admin": False})
    middleware = AgentPolicyMiddleware(context=_interactive_context(is_admin=False))
    request = SimpleNamespace(
        tool=tool,
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )

    def _capture_start(**kwargs):
        observation = original_start(**kwargs)
        observations.append(observation)
        return observation

    async def _handler(_request):
        content = await tool._arun(query="same")
        return ToolMessage(content=content, tool_call_id="call-1")

    with (
        patch.object(
            _AdminSafeReadTool,
            "is_admin_user",
            new=AsyncMock(return_value=legacy_admin),
        ),
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "start",
            side_effect=_capture_start,
        ),
    ):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    if legacy_admin:
        assert result.content == expected_result
        assert result.status == "success"
    else:
        assert json.loads(result.content) == {"success": False, "error": expected_result}
        assert result.status == "error"
    assert events.count("run") == expected_run_count
    assert len(observations) == 1
    assert observations[0].policy.effect is ActionEffect.UNKNOWN
    assert observations[0].policy.migration_state is MigrationState.LEGACY_SHADOW
    assert observations[0].decision.allowed is True
    assert observations[0].decision.shadow is True
    assert observations[0].decision.reason_code == "legacy_shadow_allow"


def test_direct_non_admin_safe_read_rejects_before_policy_or_schema() -> None:
    """
    direct 未授权请求保持原 JSON 拒绝格式且不提前触发 policy/schema。
    """
    events: list[str] = []
    orchestrator = MagicMock()
    tool = _AdminSafeReadTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    manager = MoviePilotToolsManager(
        is_admin=False,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = json.loads(asyncio.run(manager.call_tool(tool.name, {"query": "same"})))

    assert result == {
        "error": "抱歉，您没有执行此工具的权限。只有系统管理员才能执行工具操作。",
        "execution_outcome": "failed",
        "recovery": "当前身份无权执行该工具；改用允许的只读工具或请求具备权限的用户确认。",
    }
    assert events == []
    orchestrator.start.assert_not_called()


@pytest.mark.parametrize("failed_phase", ["start", "finish"])
def test_direct_manager_observation_failure_does_not_replace_success(
    failed_phase: str,
) -> None:
    """
    direct manager 的 shadow start/finish 故障不能改写真实返回值。
    """
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    getattr(orchestrator, failed_phase).side_effect = RuntimeError(f"policy-{failed_phase}-failure")
    tool = _EchoTool(session_id="session-1", user_id="user-1")
    manager = MoviePilotToolsManager(
        is_admin=True,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = asyncio.run(manager.call_tool(tool.name, {"query": "same"}))

    assert result == "same"


def test_direct_manager_fail_observation_does_not_mask_tool_error() -> None:
    """
    direct manager 的 fail hook 故障不能替换既有工具错误格式。
    """
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    orchestrator.fail.side_effect = RuntimeError("policy-fail-hook-failure")
    tool = _FailingTool(session_id="session-1", user_id="user-1")
    manager = MoviePilotToolsManager(
        is_admin=True,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = asyncio.run(manager.call_tool(tool.name, {"query": "same"}))

    assert "ValueError" in result
    assert "tool-error:same" in result
    assert "policy-fail-hook-failure" not in result


def test_direct_manager_and_agent_middleware_share_policy_resolution() -> None:
    """
    相同工具参数在 Agent 与 direct 入口应获得相同动作策略。
    """
    observations = []
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start

    def _capture_start(**kwargs):
        observation = original_start(**kwargs)
        observations.append(observation)
        return observation

    tool = _EchoTool(session_id="session-1", user_id="user-1")
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-agent",
            "name": tool.name,
            "args": {"query": "same"},
        },
    )

    async def _handler(_request):
        return ToolMessage(content="same", tool_call_id="call-agent")

    manager = MoviePilotToolsManager(
        user_id="api-user",
        session_id="api-session",
        is_admin=True,
        policy_orchestrator=DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    )
    manager.tools = [tool]

    async def _run_both():
        await middleware.awrap_tool_call(request, _handler)
        return await manager.call_tool(tool.name, {"query": "same"})

    with patch.object(
        DEFAULT_TOOL_POLICY_ORCHESTRATOR,
        "start",
        side_effect=_capture_start,
    ):
        direct_result = asyncio.run(_run_both())

    assert direct_result == "same"
    assert [item.invocation.origin for item in observations] == [
        ToolOrigin.AGENT_INTERACTIVE,
        ToolOrigin.OPERATOR_DIRECT,
    ]
    assert observations[0].policy == observations[1].policy


def test_agent_middleware_secret_setting_result_stays_out_of_receipt_logs() -> None:
    """
    Agent ToolNode 可接收管理员请求的原值，但策略回执不得记录该值。
    """
    secret_marker = "middleware-secret-setting-marker"
    executor = AsyncMock()
    executor.execute.return_value = json.dumps(
        {"success": True, "data": {"value": secret_marker}},
        ensure_ascii=False,
    )
    tool = MoviePilotApiTool(
        session_id="session-1",
        user_id="1",
        executor=executor,
    )
    tool.set_agent_context({"is_admin": True})
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-secret-setting",
            "name": tool.name,
            "args": {
                "operation_id": "config.system.get",
                "body": {
                    "setting_key": "COOKIECLOUD_KEY",
                    "show_secrets": True,
                },
            },
        },
    )
    mock_logger = MagicMock()

    async def _handler(_request):
        result = await tool._arun(
            operation_id="config.system.get",
            body={"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
        )
        return ToolMessage(content=result, tool_call_id="call-secret-setting")

    with patch("app.agent.policy.orchestrator.logger", mock_logger):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert secret_marker in result.content
    logged = "\n".join(str(call) for call in (mock_logger.debug.call_args_list + mock_logger.info.call_args_list))
    assert secret_marker not in logged
    assert '"protected_result": "***"' in logged


def test_main_agent_registers_policy_middleware_as_outermost() -> None:
    """
    主 Agent 必须把宿主策略中间件放在 middleware 链最外层。
    """
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="user-1")
    fake_llm = _AgentFactoryLLM()
    captured = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=fake_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    assert isinstance(captured["middleware"][0], AgentPolicyMiddleware)


def test_main_agent_preserves_memory_middleware_order() -> None:
    """
    策略层加入后，统一 MemoryMiddleware 仍位于摘要压缩之前。
    """
    agent = agent_module.MoviePilotAgent(
        session_id="session-1",
        user_id="user-1",
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
    )
    fake_llm = _AgentFactoryLLM()
    captured = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=fake_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    middlewares = captured["middleware"]
    policy_index = next(
        index for index, middleware in enumerate(middlewares) if isinstance(middleware, AgentPolicyMiddleware)
    )
    memory_index = next(
        index for index, middleware in enumerate(middlewares) if isinstance(middleware, MemoryMiddleware)
    )
    compaction_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, FinalRequestCompactionMiddleware)
    )
    memory_middleware = middlewares[memory_index]

    assert policy_index == 0
    assert compaction_index > memory_index
    assert memory_middleware.user_memory_dir == str(
        agent_module.agent_runtime_manager.get_user_memory_dir("user-1")
    )


_NETDISK_READ_OPERATION = "netdisk.files.list"
_NETDISK_WRITE_OPERATIONS = (
    "netdisk.files.share",
    "netdisk.files.move",
    "netdisk.files.rename",
    "netdisk.files.delete",
)
_NETDISK_ACCOUNT_ID = 6421
_NETDISK_FILE = {"file_id": "file-6421", "name": "episode.mkv"}


def _netdisk_arguments(operation_id: str) -> dict:
    """
    构造通过网盘操作输入合同的调用参数，供策略解析与真实工具执行共用。
    """
    arguments: dict = {
        "operation_id": operation_id,
        "path_params": {"account_id": _NETDISK_ACCOUNT_ID},
    }
    if operation_id == "netdisk.files.share":
        arguments["body"] = {"files": [dict(_NETDISK_FILE)]}
    elif operation_id == "netdisk.files.move":
        arguments["body"] = {"files": [dict(_NETDISK_FILE)], "target_id": "target-6421"}
    elif operation_id == "netdisk.files.rename":
        arguments["body"] = {"file": dict(_NETDISK_FILE), "name": "renamed.mkv"}
    elif operation_id == "netdisk.files.delete":
        arguments["body"] = {"files": [dict(_NETDISK_FILE)]}
    return arguments


def test_create_tools_can_disable_all_tools() -> None:
    """
    后台预选等任务可以要求工具工厂返回空列表。
    """
    from app.agent.loader import get_tool_factory

    assert get_tool_factory().create_tools(
        session_id="session-1",
        user_id="1",
        allow_tools=False,
    ) == []


def test_netdisk_admin_read_is_enforced_safe_read_and_executes_without_confirmation() -> None:
    """
    管理员读取网盘目录解析为强制执行的安全读取，直接执行且不要求确认。
    """
    arguments = _netdisk_arguments(_NETDISK_READ_OPERATION)
    executor = AsyncMock()
    executor.execute.return_value = json.dumps({"success": True, "data": []})
    tool = MoviePilotApiTool(session_id="session-1", user_id="1", executor=executor)
    tool.set_agent_context({"is_admin": True})

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_interactive_context(),
        tool=tool,
        arguments=arguments,
    )
    result = asyncio.run(tool.run(**arguments))

    assert observation.policy.effect is ActionEffect.SAFE_READ
    assert observation.policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert observation.policy.confirmation is ConfirmationMode.NONE
    assert observation.policy.result_sensitivity is ResultSensitivity.PRIVATE
    assert observation.policy.migration_state is MigrationState.ENFORCED
    assert observation.decision.allowed is True
    assert observation.decision.confirmation_required is False
    assert observation.decision.shadow is False
    assert observation.decision.reason_code == "safe_read_allow"
    assert json.loads(result) == {"success": True, "data": []}
    executor.execute.assert_awaited_once()


@pytest.mark.parametrize("operation_id", [_NETDISK_READ_OPERATION, *_NETDISK_WRITE_OPERATIONS])
def test_netdisk_non_admin_operation_is_rejected_without_leaking_account_data(operation_id: str) -> None:
    """
    非管理员读取或写入网盘都在执行前被拒绝，拒绝回执不携带账号与文件标识。
    """
    arguments = _netdisk_arguments(operation_id)
    executor = AsyncMock()
    tool = MoviePilotApiTool(session_id="session-1", user_id="1", executor=executor)
    tool.set_agent_context({"is_admin": False})

    # 网盘操作的管理员要求来自 operation 规格，而不是工具自身的 require_admin 默认值。
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name=tool.name,
        arguments=arguments,
        requires_admin=False,
    )
    result = json.loads(asyncio.run(tool.run(**arguments)))

    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert result == {
        "success": False,
        "error": "permission_denied",
        "message": "This MoviePilot API operation requires a system administrator.",
    }
    executor.execute.assert_not_awaited()
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(_NETDISK_ACCOUNT_ID) not in serialized
    assert _NETDISK_FILE["file_id"] not in serialized


@pytest.mark.parametrize("operation_id", _NETDISK_WRITE_OPERATIONS)
def test_netdisk_write_requires_confirmation_and_never_resolves_to_safe_read(operation_id: str) -> None:
    """
    网盘写操作解析为需确认的强制策略，通用编排器只能给出等待入口确认的 shadow 决定。

    通用编排器不替代各入口的确认流程：这里断言写操作不会被误判成无需确认的安全读取，
    也不会报告成已批准执行。
    """
    arguments = _netdisk_arguments(operation_id)
    tool = MoviePilotApiTool(session_id="session-1", user_id="1")
    tool.set_agent_context({"is_admin": True})

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_interactive_context(),
        tool=tool,
        arguments=arguments,
    )

    assert observation.policy.effect not in {ActionEffect.SAFE_READ, ActionEffect.SENSITIVE_READ}
    assert observation.policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert observation.policy.confirmation is ConfirmationMode.REQUIRED
    assert observation.policy.migration_state is MigrationState.ENFORCED
    assert observation.decision.shadow is True
    assert observation.decision.reason_code == "confirmation_policy_shadow_allow"


def test_netdisk_delete_is_destructive_and_requires_external_reconciliation() -> None:
    """
    删除解析为破坏性写入且只能事后核对；中断回执要求外部核对，不承诺可恢复。
    """
    arguments = _netdisk_arguments("netdisk.files.delete")
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments=arguments,
        requires_admin=True,
    )
    tool = MoviePilotApiTool(session_id="session-1", user_id="1")
    tool.set_agent_context({"is_admin": True})
    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_interactive_context(),
        tool=tool,
        arguments=arguments,
    )
    receipt = DEFAULT_TOOL_POLICY_ORCHESTRATOR.fail(observation, TimeoutError())
    description = OPERATION_DESCRIPTIONS["netdisk.files.delete"]

    assert policy.effect is ActionEffect.DESTRUCTIVE_WRITE
    assert policy.recovery is RecoveryMode.RECONCILE
    assert policy.recovery not in {
        RecoveryMode.RECOVERABLE_DELETE,
        RecoveryMode.TRANSACTION,
        RecoveryMode.BEFORE_STATE,
        RecoveryMode.IDEMPOTENT,
    }
    assert observation.policy.effect is ActionEffect.DESTRUCTIVE_WRITE
    assert observation.policy.recovery is RecoveryMode.RECONCILE
    assert observation.policy.confirmation is ConfirmationMode.REQUIRED
    assert receipt.outcome is ExecutionOutcome.UNKNOWN
    assert receipt.external_may_continue is True
    assert receipt.needs_reconcile is True
    assert "可恢复" not in description
    assert "回滚" in description


def _netdisk_write_tool(executor: AsyncMock | None = None) -> MoviePilotApiTool:
    """
    构造带管理员上下文和可观测执行器的网盘网关工具。
    """
    executor = executor or AsyncMock()
    executor.execute.return_value = json.dumps(
        {"success": True, "data": {"deleted": True}},
        ensure_ascii=False,
    )
    tool = MoviePilotApiTool(session_id="session-1", user_id="1", executor=executor)
    tool.set_agent_context({"is_admin": True})
    return tool


def _netdisk_tool_call_message(operation_id: str) -> AIMessage:
    """
    构造模型对网盘操作的单次工具调用消息。
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "moviepilot_api",
                "args": _netdisk_arguments(operation_id),
                "id": "netdisk-call-1",
                "type": "tool_call",
            }
        ],
    )


def test_netdisk_confirmation_run_requires_call_level_flag() -> None:
    """
    未获宿主确认标记的网盘写操作必须在执行器前 fail closed。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)

    result = json.loads(asyncio.run(tool.run(**_netdisk_arguments("netdisk.files.delete"))))

    assert result["success"] is False
    assert result["error"] == "confirmation_required"
    assert "未执行" in result["message"]
    executor.execute.assert_not_awaited()


def test_netdisk_confirmation_flag_allows_exactly_one_execution() -> None:
    """
    确认标记只放行一次写操作，执行后立即清除，后续调用再次被拒绝。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    arguments = _netdisk_arguments("netdisk.files.delete")

    tool.authorize_netdisk_write()
    first_result = json.loads(asyncio.run(tool.run(**arguments)))
    second_result = json.loads(asyncio.run(tool.run(**arguments)))

    assert first_result["success"] is True
    assert second_result["error"] == "confirmation_required"
    executor.execute.assert_awaited_once()


def test_netdisk_confirmation_middleware_pauses_write_before_execution() -> None:
    """
    宿主中间件在网盘写操作执行前暂停，并把确认提示交给已注册处理器。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    handler = AsyncMock(return_value="即将删除 1 个文件，请回复确认。")
    context = _interactive_context()
    context.agent_context["netdisk_write_confirmation_handler"] = handler
    middleware = AgentPolicyMiddleware(context=context, tools=[tool])

    result = asyncio.run(
        middleware.aafter_model({"messages": [_netdisk_tool_call_message("netdisk.files.delete")]}, None)
    )

    assert result["jump_to"] == "end"
    assert "已暂停" in result["messages"][0].content
    assert result["messages"][-1].content == "即将删除 1 个文件，请回复确认。"
    handler.assert_awaited_once()
    executor.execute.assert_not_awaited()


def test_netdisk_confirmation_middleware_does_not_pause_reads() -> None:
    """
    网盘只读调用不进入写操作确认暂停。
    """
    tool = _netdisk_write_tool()
    context = _interactive_context()
    context.agent_context["netdisk_write_confirmation_handler"] = AsyncMock()
    middleware = AgentPolicyMiddleware(context=context, tools=[tool])

    result = asyncio.run(
        middleware.aafter_model({"messages": [_netdisk_tool_call_message(_NETDISK_READ_OPERATION)]}, None)
    )

    assert result is None


def test_netdisk_confirmation_middleware_fails_closed_without_handler() -> None:
    """
    没有确认处理器的入口直接拒绝网盘写操作，不产生外部副作用。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    middleware = AgentPolicyMiddleware(context=_interactive_context(), tools=[tool])

    result = asyncio.run(
        middleware.aafter_model({"messages": [_netdisk_tool_call_message("netdisk.files.delete")]}, None)
    )

    assert result["jump_to"] == "end"
    assert "未执行" in result["messages"][-1].content
    executor.execute.assert_not_awaited()


def _netdisk_confirmation_agent() -> agent_module.MoviePilotAgent:
    """
    构造可处理确认控制文本的 WebAgent 会话。
    """
    return agent_module.MoviePilotAgent(
        session_id="session-1",
        user_id="1",
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
        is_channel_admin=True,
    )


def test_netdisk_confirmation_control_executes_once() -> None:
    """
    用户回复确认后仅执行一次已登记的网盘写操作。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    agent = _netdisk_confirmation_agent()
    arguments = _netdisk_arguments("netdisk.files.delete")

    prompt = asyncio.run(agent._register_netdisk_write_confirmation(tool, arguments))
    result = asyncio.run(agent._handle_secret_confirmation_control("确认", None, None, False))

    assert "确认" in prompt
    assert json.loads(result)["success"] is True
    executor.execute.assert_awaited_once()


def test_netdisk_delete_confirmation_reconciles_parent_directory() -> None:
    """
    删除确认后重新读取父目录，并把外部实际状态附在结果中。
    """
    executor = AsyncMock()
    executor.execute.side_effect = [
        json.dumps({"success": True, "data": {}}),
        json.dumps({"success": True, "data": {"files": []}}),
    ]
    tool = _netdisk_write_tool(executor)
    agent = _netdisk_confirmation_agent()
    arguments = _netdisk_arguments("netdisk.files.delete")
    arguments["body"]["files"][0]["parent_id"] = "parent-6421"

    asyncio.run(agent._register_netdisk_write_confirmation(tool, arguments))
    result = asyncio.run(agent._handle_secret_confirmation_control("确认", None, None, False))

    payload = json.loads(result)
    assert payload["success"] is True
    assert "parent-6421" in payload["reconciliation"]
    assert executor.execute.await_count == 2
    assert executor.execute.await_args_list[1].args[0] == "netdisk.files.list"


def test_netdisk_confirmation_control_cancel_does_not_execute() -> None:
    """
    用户取消后丢弃待确认调用，不执行任何网盘写入。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    agent = _netdisk_confirmation_agent()

    asyncio.run(agent._register_netdisk_write_confirmation(tool, _netdisk_arguments("netdisk.files.delete")))
    result = asyncio.run(agent._handle_secret_confirmation_control("取消", None, None, False))

    assert "已取消" in result
    executor.execute.assert_not_awaited()


def test_netdisk_confirmation_control_timeout_does_not_execute() -> None:
    """
    超过有效期的确认请求被丢弃，不执行网盘写入。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    agent = _netdisk_confirmation_agent()

    asyncio.run(agent._register_netdisk_write_confirmation(tool, _netdisk_arguments("netdisk.files.delete")))
    assert agent._pending_secret_confirmation is not None
    agent._pending_secret_confirmation.created_at = datetime.now() - timedelta(minutes=10)
    result = asyncio.run(agent._handle_secret_confirmation_control("确认", None, None, False))

    assert "过期" in result
    executor.execute.assert_not_awaited()


def test_netdisk_confirmation_registration_denies_non_admin_without_leaking_target() -> None:
    """
    非管理员在登记阶段即被拒绝，提示不得包含账号或文件标识。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    tool.set_agent_context({"is_admin": False})
    agent = agent_module.MoviePilotAgent(
        session_id="session-1",
        user_id="1",
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
        is_channel_admin=False,
    )

    message = asyncio.run(
        agent._register_netdisk_write_confirmation(tool, _netdisk_arguments("netdisk.files.delete"))
    )

    assert "无权" in message
    assert str(_NETDISK_ACCOUNT_ID) not in message
    assert _NETDISK_FILE["file_id"] not in message
    assert _NETDISK_FILE["name"] not in message
    assert agent._pending_secret_confirmation is None
    executor.execute.assert_not_awaited()


def test_netdisk_confirmation_prompt_redacts_urls_and_never_echoes_password() -> None:
    """
    确认提示把疑似链接替换为占位符，且不回显分享提取码。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    agent = _netdisk_confirmation_agent()
    arguments = {
        "operation_id": "netdisk.files.share",
        "path_params": {"account_id": _NETDISK_ACCOUNT_ID},
        "body": {
            "files": [{"file_id": "f-1", "name": "https://example.invalid/share.mkv"}],
            "password": "abcd",
            "expires_in": 86400,
        },
    }

    prompt = asyncio.run(agent._register_netdisk_write_confirmation(tool, arguments))

    assert "[链接]" in prompt
    assert "https://example.invalid" not in prompt
    assert "abcd" not in prompt
    assert str(_NETDISK_ACCOUNT_ID) in prompt


def test_netdisk_delete_confirmation_marks_directories_and_recursion() -> None:
    """
    删除目录的确认摘要必须标明目录与递归语义。
    """
    arguments = _netdisk_arguments("netdisk.files.delete")
    arguments["body"]["files"] = [
        {"file_id": "dir-1", "name": "Season 1", "is_dir": True, "parent_id": "parent-1"}
    ]

    prompt = _netdisk_confirmation_agent()._netdisk_write_target_summary(arguments)

    assert "[目录]" in prompt
    assert "递归" in prompt


def _telegram_confirmation_agent() -> agent_module.MoviePilotAgent:
    """
    构造可私聊投递结果的 Telegram 会话。
    """
    return agent_module.MoviePilotAgent(
        session_id="session-1",
        user_id="1",
        channel=NotificationChannel.Telegram.value,
        source="telegram",
        is_channel_admin=True,
    )


def _netdisk_share_result() -> str:
    """
    返回包含分享链接与提取码的网盘分享结果。
    """
    return json.dumps(
        {
            "success": True,
            "data": {"share_url": "https://pan.example/s/abc", "password": "abcd"},
        },
        ensure_ascii=False,
    )


def test_netdisk_confirmation_telegram_delivers_result_privately() -> None:
    """
    Telegram 确认执行后通过私聊交付原始结果，控制路径不返回链接或提取码。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    executor.execute.return_value = _netdisk_share_result()
    agent = _telegram_confirmation_agent()
    deliver = AsyncMock(return_value=True)

    with patch.object(agent, "_deliver_private_channel_message", deliver):
        prompt = asyncio.run(
            agent._register_netdisk_write_confirmation(
                tool, _netdisk_arguments("netdisk.files.share")
            )
        )
        result = asyncio.run(agent._handle_secret_confirmation_control("确认", None, None, False))

    assert "确认" in prompt
    assert deliver.await_count == 2
    delivered_result = deliver.await_args_list[1].args[0]
    assert "https://pan.example/s/abc" in delivered_result
    assert "abcd" in delivered_result
    assert "https://pan.example/s/abc" not in result
    assert "abcd" not in result
    assert "私聊" in result
    executor.execute.assert_awaited_once()


def test_netdisk_confirmation_telegram_delivery_failure_returns_generic_status() -> None:
    """
    私聊交付失败时只返回通用状态，不把分享链接或提取码回传到原会话。
    """
    executor = AsyncMock()
    tool = _netdisk_write_tool(executor)
    executor.execute.return_value = _netdisk_share_result()
    agent = _telegram_confirmation_agent()
    deliver = AsyncMock(side_effect=[True, False])

    with patch.object(agent, "_deliver_private_channel_message", deliver):
        asyncio.run(
            agent._register_netdisk_write_confirmation(
                tool, _netdisk_arguments("netdisk.files.share")
            )
        )
        result = asyncio.run(agent._handle_secret_confirmation_control("确认", None, None, False))

    assert "投递失败" in result
    assert "https://pan.example/s/abc" not in result
    assert "abcd" not in result
    assert deliver.await_count == 2
    executor.execute.assert_awaited_once()
