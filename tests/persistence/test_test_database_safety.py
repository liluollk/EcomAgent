"""测试库目标的安全校验(执行计划 T00)。

这些用例**不连接任何数据库**:它们证明"非测试库""缺显式授权""缺显式 URL"
三类目标在任何连接或 DROP 之前就被拒绝,以及管理连接与测试库属于同一实例。
被测实现由 ``test_database_rules`` fixture 提供,与 ``prepared_test_database``
清库前调用的是同一份代码。

用例不写 ``from conftest import ...``:``tests/persistence/conftest.py`` 与
``tests/conftest.py`` 同名,该导入会解析到前者而非被测实现。
"""

import pytest
from sqlalchemy.engine import make_url


_TEST_URL = "postgresql+asyncpg://runtime:runtime@127.0.0.1:15439/agent_runtime_refactor_test"


def _environ(rules, **overrides: str) -> dict[str, str]:
    """构造完整环境;默认是一组合法目标,用例只覆写关心的键。"""
    base = {rules.url_env: _TEST_URL, rules.reset_env: "1"}
    base.update(overrides)
    return base


def test_admin_connection_follows_the_custom_port(test_database_rules) -> None:
    """管理连接只换库名,必须留在测试库同一主机与端口,而不是 localhost:5432。"""
    rules = test_database_rules
    target = rules.resolve(_environ(rules))

    admin_url = target.admin_url
    assert admin_url.database == "postgres"
    assert admin_url.host == "127.0.0.1"
    assert admin_url.port == 15439
    assert admin_url.username == target.url.username


def test_database_name_comes_from_the_url(test_database_rules) -> None:
    """库名不再是硬编码常量:换成另一个 *_test 库即被采纳。"""
    rules = test_database_rules
    other = "postgresql+asyncpg://runtime:runtime@127.0.0.1:15439/agent_runtime_eval_run_test"
    target = rules.resolve(_environ(rules, **{rules.url_env: other}))

    assert target.database == "agent_runtime_eval_run_test"
    assert target.admin_url.database == "postgres"


def test_non_test_database_is_rejected(test_database_rules) -> None:
    """业务库即使被误配进环境变量也拒绝,不因为名字看起来可用就清理。"""
    rules = test_database_rules
    production = "postgresql+asyncpg://runtime:runtime@127.0.0.1:15439/agent_runtime"
    with pytest.raises(rules.error_type, match="_test"):
        rules.resolve(_environ(rules, **{rules.url_env: production}))


def test_missing_reset_permission_is_rejected(test_database_rules) -> None:
    """库名合法但没有显式授权标记时拒绝清理。"""
    rules = test_database_rules
    environ = _environ(rules)
    environ.pop(rules.reset_env)
    with pytest.raises(rules.error_type, match=rules.reset_env):
        rules.resolve(environ)


def test_reset_permission_must_be_exactly_one(test_database_rules) -> None:
    '''只接受 "1";"true" 这类宽松值不算显式授权。'''
    rules = test_database_rules
    with pytest.raises(rules.error_type, match=rules.reset_env):
        rules.resolve(_environ(rules, **{rules.reset_env: "true"}))


def test_missing_url_is_rejected(test_database_rules) -> None:
    """没有显式 URL 时不再回退到内置的 localhost:5432 默认库。"""
    rules = test_database_rules
    with pytest.raises(rules.error_type, match=rules.url_env):
        rules.resolve({rules.reset_env: "1"})


def test_url_without_database_is_rejected(test_database_rules) -> None:
    """URL 里没有库名时拒绝,避免把整个实例当作目标。"""
    rules = test_database_rules
    with pytest.raises(rules.error_type, match="数据库名"):
        rules.resolve(
            _environ(rules, **{rules.url_env: "postgresql+asyncpg://runtime:runtime@127.0.0.1"})
        )


def test_derive_admin_url_only_replaces_database(test_database_rules) -> None:
    """派生逻辑是纯替换,不改变方言、凭据与查询参数。"""
    rules = test_database_rules
    url = make_url("postgresql+asyncpg://runtime:secret@db.internal:6543/shop_test?ssl=true")
    admin = rules.derive_admin_url(url)

    assert admin.database == "postgres"
    assert admin.drivername == url.drivername
    assert admin.password == url.password
    assert admin.query == url.query
