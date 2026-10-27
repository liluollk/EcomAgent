"""入口装配测试:生产入口与演示入口的差别只在代码,不在环境变量。

覆盖三件事:
1. 生产入口不注册种子路由,演示入口注册;
2. 生产入口不装配执行器(未传 driver 工厂时不推进);
3. 启动配置缺失时直接失败,不回退到默认密钥。
"""

import base64

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.demo import create_demo_app
from apps.api.main import create_app
from apps.settings import StartupConfigurationError, StartupSettings


_JWT_ENV = "RUNTIME_JWT_SECRET"
_MASTER_KEY_ENV = "RUNTIME_MASTER_KEY"
_DATABASE_URL_ENV = "RUNTIME_DATABASE_URL"


def _valid_master_key() -> str:
    return base64.b64encode(b"0" * 32).decode()


def test_production_entry_has_no_seed_route() -> None:
    """生产入口的暴露面里不存在种子接口。"""
    paths = set(create_app().openapi()["paths"])

    assert "/api/e2e/seed" not in paths


def test_demo_entry_registers_seed_route() -> None:
    """演示入口注册种子接口;差别由传入的路由决定。"""
    paths = set(create_demo_app().openapi()["paths"])

    assert "/api/e2e/seed" in paths


def test_production_entry_assembles_no_executor() -> None:
    """未传 driver 工厂时不装配执行器:API 只创建 Run,不伪装成已推进。"""
    app = create_app()

    assert app.state.driver_factory is None


def test_demo_entry_assembles_scripted_driver() -> None:
    """演示入口显式给出脚本 driver 工厂。"""
    app = create_demo_app()

    assert app.state.driver_factory is not None


def test_startup_settings_require_every_value() -> None:
    """三个启动值缺任一都失败,且错误消息指明缺的是哪个变量。"""
    complete = {
        _DATABASE_URL_ENV: "postgresql+asyncpg://runtime:runtime@127.0.0.1:5432/agent_runtime",
        _JWT_ENV: "env-secret-value",
        _MASTER_KEY_ENV: _valid_master_key(),
    }

    settings = StartupSettings.from_env(complete)
    assert settings.jwt_secret == "env-secret-value"

    for missing in (_DATABASE_URL_ENV, _JWT_ENV, _MASTER_KEY_ENV):
        partial = {key: value for key, value in complete.items() if key != missing}
        with pytest.raises(StartupConfigurationError):
            StartupSettings.from_env(partial)


def test_master_key_is_not_in_repr() -> None:
    """主密钥不随 repr 输出,避免经日志或异常泄漏。"""
    settings = StartupSettings.from_env(
        {
            _DATABASE_URL_ENV: "postgresql+asyncpg://runtime:runtime@127.0.0.1:5432/agent_runtime",
            _JWT_ENV: "env-secret-value",
            _MASTER_KEY_ENV: _valid_master_key(),
        }
    )

    assert _valid_master_key() not in repr(settings)


async def test_lifespan_refuses_to_start_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺密钥时 lifespan 直接失败,服务不会开始接收请求。"""
    monkeypatch.setenv(_DATABASE_URL_ENV, "postgresql+asyncpg://runtime:runtime@127.0.0.1:5432/x")
    monkeypatch.delenv(_JWT_ENV, raising=False)
    monkeypatch.setenv(_MASTER_KEY_ENV, _valid_master_key())
    app = create_app()

    with pytest.raises(StartupConfigurationError):
        async with app.router.lifespan_context(app):
            pass


async def test_injected_engine_does_not_require_environment(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调用方注入引擎即表示自己负责配置,不再读启动环境变量。"""
    for name in (_DATABASE_URL_ENV, _JWT_ENV, _MASTER_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    app = create_demo_app(engine=engine)

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
