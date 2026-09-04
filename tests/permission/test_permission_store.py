"""测试 PermissionStore — remember/forget/过期。"""

import time
import pytest
from permission.permission_store import PermissionStore


def test_permission_store_remember():
    store = PermissionStore()
    store.remember("tool:update_price:SKU-001", ttl_minutes=30)
    assert store.is_remembered("tool:update_price:SKU-001")


def test_permission_store_forget():
    store = PermissionStore()
    store.remember("tool:update_price:SKU-001")
    store.forget("tool:update_price:SKU-001")
    assert not store.is_remembered("tool:update_price:SKU-001")


def test_permission_store_not_remembered():
    store = PermissionStore()
    assert not store.is_remembered("nonexistent")


def test_permission_store_expiry():
    store = PermissionStore()
    # 使用 0 分钟 TTL，立即过期
    store.remember("tool:update_price:SKU-001", ttl_minutes=0)
    time.sleep(0.01)  # 等待过期
    assert not store.is_remembered("tool:update_price:SKU-001")


def test_permission_store_forget_all():
    store = PermissionStore()
    store.remember("key1")
    store.remember("key2")
    store.forget_all()
    assert not store.is_remembered("key1")
    assert not store.is_remembered("key2")