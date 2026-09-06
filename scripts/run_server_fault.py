"""本地演示 launcher：进程内装载确定性故障脚本 + 演示快策略后启动 transport 服务。

用法：python scripts/run_server_fault.py [script]
  script 可选（mock_commerce/fault_injection._FAULT_SCRIPTS）：
    rate_limit_once_then_success  首次 429 → 重试成功（E14 现场版）
    timeout_once_then_success     写副作用落库后挂起 > 写超时 → 重试命中幂等回放（E17 现场版）
    normal / 缺省                 不注入故障，正常服务

演示快策略（与 harness 同思路，仅本进程生效）：写超时 2s + 退避 0.5s，
使超时故障（睡眠 3s）能在浏览器演示里秒级复现「重试 + 幂等回放」。
"""

import os
import sys

# 支持 `python scripts/run_server_fault.py` 直接运行：把仓库根加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

script = sys.argv[1] if len(sys.argv) > 1 else "normal"

from execution.policy import ExecutionPolicy, TimeoutPolicy, configure_default_policy
from execution.policy.timeout import TimeoutConfig

if script and script != "normal":
    from mock_commerce import fault_injection

    if script == "timeout_once_then_success":
        # 写超时 2s < 故障睡眠 3s：客户端必然超时，重试复用幂等键 → 回放
        configure_default_policy(ExecutionPolicy(timeout=TimeoutPolicy(TimeoutConfig(default_seconds=5.0, read_seconds=2.0, write_seconds=2.0))))
        fault_injection.load_script(script, timeout_seconds=3.0)
    else:
        fault_injection.load_script(script)
    print(f"[demo] 故障脚本已装载: {script}")
else:
    print("[demo] 无故障注入，正常服务")

import uvicorn

from transport.server import app

uvicorn.run(app, host="127.0.0.1", port=8000)
