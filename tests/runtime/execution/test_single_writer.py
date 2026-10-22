"""单写者锁测试(ADR-003 §5.2)。

验证的是部署边界,不是业务语义:同一数据库上第二个执行器必须在启动时
fail-fast,而不是降级为"只读"或与第一个执行器并发推进。

锁是 PostgreSQL advisory lock,挂在独立连接上;本测试不写任何业务行。
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.execution.single_writer import SingleWriterLock, SingleWriterViolation


async def test_second_executor_is_rejected_while_lock_is_held(engine: AsyncEngine) -> None:
    """第一个执行器持锁时,第二个执行器的 acquire 必须失败。"""
    first = SingleWriterLock(engine)
    second = SingleWriterLock(engine)
    await first.acquire()
    try:
        assert first.held is True
        try:
            await second.acquire()
        except SingleWriterViolation:
            pass
        else:
            raise AssertionError("a second executor must not acquire the same lock")
        assert second.held is False
    finally:
        await first.release()
    assert first.held is False


async def test_lock_is_reusable_after_release(engine: AsyncEngine) -> None:
    """释放后同一进程可重新获取(启动 → 关闭 → 再启动的场景)。"""
    lock = SingleWriterLock(engine)
    await lock.acquire()
    await lock.release()
    await lock.acquire()
    try:
        assert lock.held is True
    finally:
        await lock.release()


async def test_release_without_acquire_is_a_noop(engine: AsyncEngine) -> None:
    """未持有时 release 不报错(关闭路径可能被重复调用)。"""
    lock = SingleWriterLock(engine)
    await lock.release()
    assert lock.held is False


async def test_reacquire_within_one_instance_is_rejected(engine: AsyncEngine) -> None:
    """同一实例重复 acquire 是编程错误,不应静默成功。"""
    lock = SingleWriterLock(engine)
    await lock.acquire()
    try:
        try:
            await lock.acquire()
        except RuntimeError:
            pass
        else:
            raise AssertionError("acquiring an already-held lock must raise")
    finally:
        await lock.release()
