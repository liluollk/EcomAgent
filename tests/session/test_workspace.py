"""测试 Workspace 创建和 Source 管理。"""

import pytest
from session.workspace import Workspace


def test_workspace_creation():
    ws = Workspace(workspace_id="ws-001", name="OceanBreeze")
    assert ws.workspace_id == "ws-001"
    assert ws.name == "OceanBreeze"
    assert ws.sources == []


def test_workspace_add_source():
    ws = Workspace(workspace_id="ws-001", name="test")
    ws.add_source("taobao")
    assert "taobao" in ws.sources
    assert len(ws.sources) == 1


def test_workspace_remove_source():
    ws = Workspace(workspace_id="ws-001", name="test")
    ws.add_source("taobao")
    ws.add_source("jd")
    ws.remove_source("taobao")
    assert "taobao" not in ws.sources
    assert "jd" in ws.sources


def test_workspace_has_source():
    ws = Workspace(workspace_id="ws-001", name="test")
    ws.add_source("douyin")
    assert ws.has_source("douyin")
    assert not ws.has_source("taobao")


def test_workspace_duplicate_source():
    ws = Workspace(workspace_id="ws-001", name="test")
    ws.add_source("taobao")
    ws.add_source("taobao")
    assert len(ws.sources) == 1


def test_workspace_metadata():
    ws = Workspace(
        workspace_id="ws-001",
        name="test",
        metadata={"brand": "OceanBreeze", "category": "家居卫浴"},
    )
    assert ws.metadata["brand"] == "OceanBreeze"