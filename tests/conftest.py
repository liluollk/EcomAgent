"""pytest conftest — 将项目根目录添加到 sys.path + 结果存档（L4 回归基线）。"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 将项目根目录添加到 Python 模块搜索路径
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 测试进程使用独立持久化目录，避免污染真实数据与跨用例串扰
os.environ.setdefault("AGENT_STORAGE_DIR", tempfile.mkdtemp(prefix="agent-test-"))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """把本次运行结果存档为 data/test-reports/latest.json。

    L4 回归基线：每次全量/子集运行后覆盖 latest.json，两次运行对比
    passed/failed 即可看出改动是否引入行为退化（多工具/多分支 diff 的
    最小实现；未来可加 CI 门禁与历史趋势）。
    """
    stats = terminalreporter.stats
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "exitstatus": int(exitstatus),
        "passed": len(stats.get("passed", [])),
        "failed": len(stats.get("failed", [])),
        "errors": len(stats.get("error", [])),
        "skipped": len(stats.get("skipped", [])),
        "xfailed": len(stats.get("xfailed", [])),
        "duration_seconds": round(time.time() - terminalreporter._sessionstarttime, 2),
    }
    report_dir = project_root / "data" / "test-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )