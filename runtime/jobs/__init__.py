"""RunJob 租约执行内核(计划 02 Task 4;架构文档 §8)。

- ``lease_service``:领取/心跳/完成(fencing)/释放;
- ``worker``:claim → 执行回调 → complete 的最小 Worker 骨架;
- ``sweeper``:过期租约回收与 RECONCILE_OPERATION 决策。
"""
