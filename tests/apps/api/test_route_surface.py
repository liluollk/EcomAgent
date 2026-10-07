"""产品 API 暴露面测试。"""

from apps.api.main import create_app


def test_product_api_does_not_expose_a_global_agent_catalog() -> None:
    paths = set(create_app().openapi()["paths"])

    assert "/api/agents" not in paths
