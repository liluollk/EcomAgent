"""演示入口:脚本模型 + Mock 平台 + 种子接口。

与生产入口 ``apps/api/main.py`` 的差别全部写在这个文件里,不靠环境变量选择:

- driver 用 :class:`apps.scripted_e2e.ProductDriverRegistry`(脚本模型 + Mock 平台);
- 额外注册 ``/api/e2e/seed`` 种子路由。

启动哪个模块就装哪套东西,compose 的 ``command`` 一眼可见,没有"当前是什么
模式"这种需要推断的状态。生产需要的密钥与数据库配置仍然照常校验。
"""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.e2e import router as e2e_router
from apps.api.executor_host import DriverFactory
from apps.api.main import create_app
from apps.scripted_e2e import ProductDriverRegistry
from apps.settings import StartupSettings


def _scripted_driver_factory(engine: AsyncEngine) -> ProductDriverRegistry:
    return ProductDriverRegistry(engine)


def create_demo_app(
    *,
    engine: AsyncEngine | None = None,
    settings: StartupSettings | None = None,
) -> FastAPI:
    """装配演示应用:脚本 driver + 种子路由。"""
    driver_factory: DriverFactory = _scripted_driver_factory
    return create_app(
        engine=engine,
        settings=settings,
        driver_factory=driver_factory,
        extra_routers=(e2e_router,),
    )


# uvicorn 入口:uvicorn apps.api.demo:app
app = create_demo_app()
