"""版本化发布 Gate:基线派生与发布审查。

- :mod:`eval.gates.schema`::class:`~eval.gates.schema.ReleaseGate` 与
  :class:`~eval.gates.schema.GateResult` 的固定(frozen)结构;
- :mod:`eval.gates.derive_baseline`:从首次合格的 Core 报告派生
  Gate 并写出门文件(如 ``eval/gates/v1.json``)——门只能派生一次;
- :mod:`eval.gates.check_release`:以固定 Gate 审查报告(CLI 退出码:
  通过 0 / 失败 1 / 前置错误 2)。

``eval/gates/v1.json`` 只能从协议有效且无 Infrastructure Error 的 Core
报告派生一次;本包不携带任何编造的 Gate 文件。
"""
