"""测试 MCP server 配置注册表 — 默认值、CRUD、校验、to_params 环境合并。"""

import pytest

import sources.mcp_server_config as msc
from sources.mcp_server_config import McpServerRegistry, to_params


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """隔离配置文件与模块级缓存。"""
    monkeypatch.setenv("MCP_SERVERS_CONFIG_FILE", str(tmp_path / "mcp_servers.json"))
    msc._store._mtime = -1
    msc._store._servers = None
    yield
    msc._store._mtime = -1
    msc._store._servers = None


def test_default_when_no_file():
    reg = McpServerRegistry()
    servers = reg.list()
    assert len(servers) == 1
    assert servers[0]["name"] == "external-demo"
    assert servers[0]["enabled"] is True


def test_add_update_remove():
    reg = McpServerRegistry()
    added = reg.add({"name": "my-tools", "command": "python", "args": ["-m", "x"], "env": {"K": "V"}})
    assert added["name"] == "my-tools"
    assert {s["name"] for s in reg.list()} == {"external-demo", "my-tools"}

    updated = reg.update("my-tools", {"enabled": False})
    assert updated["enabled"] is False
    assert "my-tools" not in [s["name"] for s in reg.enabled_configs()]

    reg.remove("my-tools")
    assert reg.get("my-tools") is None


def test_add_validation():
    reg = McpServerRegistry()
    with pytest.raises(ValueError):
        reg.add({"name": "", "command": "python"})
    with pytest.raises(ValueError):
        reg.add({"name": "x", "command": ""})
    with pytest.raises(ValueError):
        reg.add({"name": "external-demo", "command": "python"})  # 重名
    reg.add({"name": "x", "command": "python"})
    with pytest.raises(ValueError):
        reg.add({"name": "x", "command": "python"})


def test_update_and_remove_unknown_raise():
    reg = McpServerRegistry()
    with pytest.raises(KeyError):
        reg.update("nope", {"enabled": False})
    with pytest.raises(KeyError):
        reg.remove("nope")


def test_to_params_merges_env():
    params = to_params({"command": "python", "args": ["-c", "x"], "env": {"EXTRA": "1"}})
    assert params.command == "python"
    assert params.args == ["-c", "x"]
    assert params.env["EXTRA"] == "1"
    # 进程环境被继承（子进程可见 CHANNEL_API_URL 等）
    assert "PATH" in params.env or len(params.env) > 1