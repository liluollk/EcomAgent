"""离线评测 Harness — 行为契约评测的运行基座。

与业务引擎分层：engine 负责执行，harness 负责「准备任务 → 驱动运行 →
记录轨迹 → 评测验收」，评测集（场景表）与引擎代码分离，新增场景无需
改动引擎或断言逻辑。

组件：
    cases.py      评测任务集（行为契约场景数据表，单一事实源）
    assertions.py 期望轨迹断言器（与引擎层测试共用同一套规则）
    hooks.py      场景前置/收尾钩子（动态渠道、跨会话记忆落盘验证）
    runner.py     运行器（生产装配驱动，事件流 trace 全量记录）
    scheduler.py  调度（顺序执行、case 级超时、失败重试）
    recorder.py   轨迹记录（每次运行一个 run_id 目录，trace.jsonl）
    metrics.py    指标汇总（通过率、失败分类）
    report.py     验收报告 + baseline 指纹对比（退化即非零退出码）

入口：
    python -m harness [--save-baseline] [--case NAME] [--timeout N] [--retries N]
"""

__version__ = "1.0.0"