"""外部集成 Outbox(计划 04 Task 7)。

- ``outbox_worker``:通用短租约 Outbox Worker(领取/处理/延迟重试/DEAD),
  与 RunJob 租约同型,但绝不读取或修改任何 Run 状态。
"""
