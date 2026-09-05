"""行为契约评测集（L3）— 数据表迁移至 harness/cases.py（单一事实源）。

本文件保留为兼容层：pytest e2e 驱动器（tests/e2e/test_e2e_demo.py）从
此处取 SCENARIOS，独立入口（python -m harness）与 pytest 共用同一数据表。
新增场景请直接编辑 harness/cases.py。
"""

from harness.cases import SCENARIOS  # noqa: F401