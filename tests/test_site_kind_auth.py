"""
验证站点类型与认证方式的校验、CD 站点新增分支及密码不回显约定。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.site.mutation import SiteMutationCommand
from app.schemas.site import Site, SiteInput


def _command(**overrides):
    """
    构造可观察站点写用例及其依赖。

    :param overrides: 覆盖默认依赖
    :return: 站点写命令与依赖字典
    """
    repository = Mock()
    repository.get_by_id = AsyncMock(
        return_value=SimpleNamespace(
            domain="demo.example",
            kind="pt",
            auth_type="cookie",
        )
    )
    repository.async_get_by_domain = AsyncMock(return_value=None)
    repository.stage_create = AsyncMock()
    repository.stage_update = AsyncMock(return_value=True)
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    dependencies = {
        "repository": repository,
        "unit_of_work": unit_of_work,
        "auth_level_provider": Mock(return_value=2),
        "indexer_loader": AsyncMock(return_value={"name": "Demo", "public": True}),
        "domain_extractor": lambda value: "demo.example",
        "url_normalizer": lambda value: "https://demo.example/",
        "publish_updated": AsyncMock(),
        "publish_deleted": AsyncMock(),
    }
    dependencies.update(overrides)
    return SiteMutationCommand(**dependencies), dependencies


@pytest.mark.asyncio
async def test_create_pt_site_uses_defaults_and_indexer_loader():
    """
    PT 站点未显式声明类型时使用 pt/cookie 默认值，并仍走资源校验。
    """
    command, dependencies = _command()

    result = await command.create({"url": "https://demo.example/path"})

    assert result.success is True
    dependencies["indexer_loader"].assert_awaited_once_with("demo.example")
    mutation = dependencies["repository"].stage_create.await_args.args[0]
    assert mutation.values["kind"] == "pt"
    assert mutation.values["auth_type"] == "cookie"
    assert mutation.values["name"] == "Demo"
    assert mutation.values["public"] == 1


@pytest.mark.asyncio
async def test_create_cd_site_skips_indexer_loader_and_normalizes_fields():
    """
    CD 站点跳过资源校验，保留请求名称、公开状态与默认超时。
    """
    command, dependencies = _command()

    result = await command.create(
        {
            "url": "https://demo.example/path",
            "name": "网盘站",
            "kind": "cd",
            "auth_type": "auth",
            "username": "user",
            "password": "secret",
            "public": 1,
            "timeout": None,
        }
    )

    assert result.success is True
    dependencies["indexer_loader"].assert_not_awaited()
    mutation = dependencies["repository"].stage_create.await_args.args[0]
    assert mutation.values["kind"] == "cd"
    assert mutation.values["auth_type"] == "auth"
    assert mutation.values["name"] == "网盘站"
    assert mutation.values["public"] == 0
    assert mutation.values["timeout"] == 90
    assert mutation.values["auth_refresh_interval"] == 300


@pytest.mark.asyncio
async def test_create_cd_site_falls_back_to_domain_name():
    """
    CD 站点未提供名称时以规范域名作为显示名称。
    """
    command, dependencies = _command()

    result = await command.create(
        {
            "url": "https://demo.example/path",
            "kind": "cd",
            "auth_type": "auth",
            "username": "user",
            "password": "secret",
        }
    )

    assert result.success is True
    mutation = dependencies["repository"].stage_create.await_args.args[0]
    assert mutation.values["name"] == "demo.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"username": "", "password": "secret"}, "CD 站点必须填写用户名"),
        ({"username": "user", "password": ""}, "CD 站点必须填写密码"),
        ({"username": "user"}, "CD 站点必须填写密码"),
    ],
)
async def test_create_cd_site_requires_username_and_password(payload, message):
    """
    CD 站点缺少用户名或密码时拒绝创建且不写库。
    """
    command, dependencies = _command()

    result = await command.create(
        {
            "url": "https://demo.example/path",
            "kind": "cd",
            "auth_type": "auth",
            **payload,
        }
    )

    assert result.success is False
    assert result.message == message
    dependencies["indexer_loader"].assert_not_awaited()
    dependencies["repository"].stage_create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "auth_type"),
    [
        ("pt", "auth"),
        ("cd", "cookie"),
        ("cd", "api"),
        ("unknown", "cookie"),
        ("pt", "unknown"),
    ],
)
async def test_create_rejects_invalid_kind_auth_combination(kind, auth_type):
    """
    站点类型与认证方式组合越界时返回明确的中文原因。
    """
    command, dependencies = _command()

    result = await command.create(
        {
            "url": "https://demo.example/path",
            "kind": kind,
            "auth_type": auth_type,
            "username": "user",
            "password": "secret",
        }
    )

    assert result.success is False
    assert result.message
    dependencies["repository"].stage_create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["", None])
async def test_update_ignores_empty_password(password):
    """
    更新时空字符串或 None 密码不得覆盖已保存的密码。
    """
    command, dependencies = _command()

    result = await command.update(
        {
            "id": 7,
            "url": "https://demo.example/path",
            "password": password,
        }
    )

    assert result.success is True
    mutation = dependencies["repository"].stage_update.await_args.args[1]
    assert "password" not in mutation.values


@pytest.mark.asyncio
async def test_update_keeps_existing_kind_and_auth_type_when_omitted():
    """
    更新未提供类型与认证方式时沿用站点已有取值。
    """
    repository = Mock()
    repository.get_by_id = AsyncMock(
        return_value=SimpleNamespace(
            domain="demo.example",
            kind="cd",
            auth_type="auth",
        )
    )
    repository.stage_update = AsyncMock(return_value=True)
    command, dependencies = _command(repository=repository)

    result = await command.update(
        {
            "id": 7,
            "url": "https://demo.example/path",
            "password": "new-secret",
        }
    )

    assert result.success is True
    mutation = dependencies["repository"].stage_update.await_args.args[1]
    assert mutation.values["kind"] == "cd"
    assert mutation.values["auth_type"] == "auth"
    assert mutation.values["password"] == "new-secret"


@pytest.mark.asyncio
async def test_update_rejects_invalid_auth_combination():
    """
    更新时同样拒绝越界的类型与认证方式组合。
    """
    command, dependencies = _command()

    result = await command.update(
        {
            "id": 7,
            "url": "https://demo.example/path",
            "kind": "cd",
            "auth_type": "cookie",
        }
    )

    assert result.success is False
    dependencies["repository"].stage_update.assert_not_awaited()


def test_site_dto_never_exposes_password():
    """
    响应 DTO 不含密码字段，只有请求 DTO 接收密码。
    """
    assert "password" not in Site.model_fields
    assert "password" in SiteInput.model_fields
    assert "password" not in Site.model_validate(
        {
            "id": 1,
            "name": "Demo",
            "url": "https://demo.example/",
            "kind": "cd",
            "auth_type": "auth",
            "password": "secret",
        }
    ).model_dump()


@pytest.mark.asyncio
async def test_create_cd_site_skips_user_auth_level_gate():
    """
    CD 站点不依赖官方站点资源包，未通过用户认证时仍可新增。
    """
    command, dependencies = _command(auth_level_provider=Mock(return_value=1))

    result = await command.create(
        {
            "url": "http://cloudsaver.example:8008",
            "kind": "cd",
            "auth_type": "auth",
            "name": "网盘站点",
            "username": "admin",
            "password": "secret",
        }
    )

    assert result.success is True
    dependencies["repository"].stage_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_pt_site_still_requires_user_auth_level():
    """
    PT 站点继续受用户认证门槛约束，未认证时拒绝新增。
    """
    command, dependencies = _command(auth_level_provider=Mock(return_value=1))

    result = await command.create({"url": "https://demo.example/path"})

    assert result.success is False
    assert "用户未通过认证" in (result.message or "")
    dependencies["repository"].stage_create.assert_not_awaited()
