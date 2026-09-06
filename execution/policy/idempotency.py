"""
IdempotencyPolicy — 写操作幂等键生成与传播。

格式: idem_{session_id}_t{turn}_{tool_name}_{params_hash}。
同一幂等键的重复请求由 Mock API 端返回首次结果，不重复执行副作用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class IdempotencyConfig:
    enabled: bool = True
    key_prefix: str = "idem"


@dataclass
class IdempotencyPolicy:
    config: IdempotencyConfig = field(default_factory=IdempotencyConfig)

    def generate_key(
        self,
        session_id: str,
        turn: int,
        tool_name: str,
        params: dict,
    ) -> str:
        params_canon = json.dumps(params, sort_keys=True, ensure_ascii=False)
        params_hash = hashlib.sha256(params_canon.encode()).hexdigest()[:8]
        return f"{self.config.key_prefix}_{session_id}_t{turn}_{tool_name}_{params_hash}"

    def should_attach(self, tool_name: str) -> bool:
        if not self.config.enabled:
            return False
        # load_skill 是元工具（仅加载上下文，无外部副作用），与权限层口径一致
        if tool_name == "load_skill":
            return False
        read_prefixes = ("query_", "get_", "list_", "search_")
        return not any(tool_name.startswith(p) for p in read_prefixes)