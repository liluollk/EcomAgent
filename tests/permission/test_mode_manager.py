"""测试 ModeManager — safe/ask/execute 三模式。"""

import pytest
from permission.mode_manager import ModeManager, PermissionMode


def test_mode_manager_default():
    manager = ModeManager()
    assert manager.mode == PermissionMode.ASK


def test_mode_manager_set_mode():
    manager = ModeManager()
    manager.set_mode(PermissionMode.EXECUTE)
    assert manager.mode == PermissionMode.EXECUTE
    manager.set_mode(PermissionMode.READONLY)
    assert manager.mode == PermissionMode.READONLY


def test_mode_manager_safe_mode():
    manager = ModeManager(PermissionMode.READONLY)
    assert not manager.is_write_allowed()
    assert not manager.is_confirmation_required()
    assert not manager.is_auto_execute()


def test_mode_manager_ask_mode():
    manager = ModeManager(PermissionMode.ASK)
    assert manager.is_write_allowed()
    assert manager.is_confirmation_required()
    assert not manager.is_auto_execute()


def test_mode_manager_execute_mode():
    manager = ModeManager(PermissionMode.EXECUTE)
    assert manager.is_write_allowed()
    assert not manager.is_confirmation_required()
    assert manager.is_auto_execute()