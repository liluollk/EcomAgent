"""Runtime Repository 集合(计划 01 Task 4)。

每个 Repository 接收一个 ``AsyncSession``;只执行 flush 与条件更新,
绝不调用 commit —— 事务边界由 ``runtime.persistence.unit_of_work`` 的
UnitOfWork 拥有(架构文档 §13)。
"""
