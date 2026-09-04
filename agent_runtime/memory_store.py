"""长期记忆 — Claude Code 式：MEMORY.md 索引 + 独立记忆文件（简单实现）。

存储结构（对应 ~/.claude/projects/<项目>/memory/）:
    data/memory/<workspace_id>/
        MEMORY.md            # 索引：文件链接 + 简述 + 类型
        <slug>.md            # 独立记忆：frontmatter(name/description/type) + 正文

记忆类型（四种）：
    user      用户偏好
    feedback  用户纠正
    project   项目决策
    reference 外部参考

工作流（简单实现，检索的"模型智能筛选"先用关键词匹配，接口可替换）：
- 写入：
    1. 自动提取：每个产生工具调用的 turn 结束，沉淀为 project（用户纠正
       语气时 feedback）类记忆文件，并重建索引。
    2. 显式："记住…" / "请记住…" → 创建或更新记忆文件（含 user 偏好归类）；
       "记到参考…" / "作为参考…" / "记住reference…" → 写入 reference 类型。
- 遗忘："忘记…" → 删除匹配的记忆文件并重建索引。
- 检索：新 turn 构建系统提示词时注入 MEMORY.md 索引（前 N 行）+ 与当前
  请求关键词命中的最多 5 个记忆文件全文。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

# 每种记忆类型的可接受值
MEMORY_TYPES = ("user", "feedback", "project", "reference")
DEFAULT_TYPE = "project"

# 注入时最多加载的独立记忆文件数（对应 Claude Code 的"最多 5 个"）
MAX_LOAD_FILES = 5
# 索引注入行数上限（对应前 N 行）
INDEX_MAX_LINES = 200

# 用户纠正语气词：命中的自动提取记忆归类为 feedback
_CORRECTION_WORDS = ("不要", "不应该", "别这样", "错了", "改为别", "以后别", "禁止")


def _memory_root() -> str:
    return os.environ.get("MEMORY_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "memory")
    )


def _slug(name: str) -> str:
    """文件名化：非字母数字 → 下划线，统一小写。"""
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", name).strip("_")
    return s[:60] or "mem"


class MemoryStore:
    """索引文件 + 独立记忆文件的内存/磁盘实现。"""

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = root or _memory_root()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def _ws_dir(self, workspace_id: str) -> str:
        return os.path.join(self._root, workspace_id)

    def _index_path(self, workspace_id: str) -> str:
        return os.path.join(self._ws_dir(workspace_id), "MEMORY.md")

    def _file_path(self, workspace_id: str, slug: str) -> str:
        return os.path.join(self._ws_dir(workspace_id), f"{slug}.md")

    # ------------------------------------------------------------------
    # 写入：自动提取 + 显式记住 / 忘记
    # ------------------------------------------------------------------

    def handle_turn(
        self,
        workspace_id: str,
        user_message: str,
        messages: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[str, str]:
        """turn 结束入口：显式指令优先，否则自动提取沉淀。返回 (action, name)。

        Args:
            workspace_id: 工作区标识。
            user_message: 用户本轮请求（含"记住/忘记"时走显式分支）。
            messages: 组装后的消息列表（自动提取时需要；可缺省）。

        Returns:
            (action, name)：action ∈ store/updated/forgot/skip。
        """
        explicit = self._try_explicit(workspace_id, user_message)
        if explicit is not None:
            return explicit
        return self._auto_extract(workspace_id, user_message, messages or [])

    def remember(self, workspace_id: str, text: str, mem_type: str = DEFAULT_TYPE) -> str:
        """显式记忆：创建或更新一个记忆文件并重建索引。返回记忆名。"""
        name = text.strip().splitlines()[0][:30] if text.strip() else "记忆"
        body = (
            f"---\nname: {name}\ndescription: {text[:80]}\ntype: {mem_type}\n---\n\n"
            "## What\n"
            f"{text}\n\n"
            "## Why / How to apply\n"
            "- 来源：用户显式要求记住。\n"
            "- 应用：后续同类请求直接采用本条记忆。\n"
        )
        self._write_memory_file(workspace_id, name, body)
        self.refresh_index(workspace_id)
        return name

    def forget(self, workspace_id: str, keyword: str) -> list[str]:
        """遗忘：删除名称或正文含关键词的记忆文件并重建索引，返回删除的文件名。"""
        removed: list[str] = []
        for fname in self._list_memory_files(workspace_id):
            if keyword in fname or keyword in self._read_memory_file(workspace_id, fname):
                try:
                    os.remove(os.path.join(self._ws_dir(workspace_id), fname))
                    removed.append(fname)
                except OSError:
                    pass
        if removed:
            self.refresh_index(workspace_id)
        return removed

    # ------------------------------------------------------------------
    # 自动提取
    # ------------------------------------------------------------------

    def _auto_extract(
        self, workspace_id: str, user_message: str, messages: list[dict[str, Any]]
    ) -> tuple[str, str]:
        tool_names: list[str] = []
        last_user = user_message or ""
        last_result = ""
        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    name = str(tc.get("function", {}).get("name", "") or "?")
                    if name not in tool_names:
                        tool_names.append(name)
            elif m.get("role") == "tool" and m.get("content"):
                last_result = str(m["content"])
        if not tool_names:
            return ("skip", "")
        mem_type = (
            "feedback"
            if any(w in user_message for w in _CORRECTION_WORDS)
            else DEFAULT_TYPE
        )
        digest = {
            "目标": last_user[:80],
            "进度": last_result[:140] or "—",
            "工具": "、".join(tool_names),
        }
        name = f"经营记录_{time.strftime('%m-%d_%H:%M')}_{mem_type}"
        desc = f"用户请求 {digest['目标'][:40]}"
        lines = "\n".join(f"- {k}：{v}" for k, v in digest.items())
        body = (
            f"---\nname: {name}\ndescription: {desc}\ntype: {mem_type}\n---\n\n"
            "## What\n"
            f"{lines}\n\n"
            "## Why / How to apply\n"
            "- 来源：自动提取（对话结束时分析沉淀）。\n"
            "- 应用：后续同类请求参考该轮决策与进度，避免重复排查。\n"
        )
        self._write_memory_file(workspace_id, name, body)
        self.refresh_index(workspace_id)
        return ("store", name)

    # ------------------------------------------------------------------
    # 显式指令解析
    # ------------------------------------------------------------------

    def _try_explicit(self, workspace_id: str, user_message: str) -> Optional[tuple[str, str]]:
        # reference 前缀优先于通用"记住"：显式归类为外部参考（而不是 project/user）
        m = re.search(
            r"(?:记到参考|作为参考|记住reference|记住参考|记一下参考)\s*[:：，,]?\s*(.+)",
            user_message,
            re.IGNORECASE,
        )
        if m:
            text = m.group(1).strip().rstrip("。.")
            if not text:
                return None
            return (
                "updated" if self._name_exists(workspace_id, text[:60]) else "store",
                self.remember(workspace_id, text, "reference"),
            )
        m = re.search(r"(?:记住|请记住|记下|记得)\s*[:：，,]?\s*(.+)", user_message)
        if m:
            text = m.group(1).strip().rstrip("。.")
            if not text:
                return None
            mem_type = "user" if re.search(r"(?:我|我的|偏好|喜欢|习惯)", text) else DEFAULT_TYPE
            return ("updated" if self._name_exists(workspace_id, text[:60]) else "store", self.remember(workspace_id, text, mem_type))
        m = re.search(r"(?:忘记|忘了|删除记忆)\s*[:：，,]?\s*(.+)", user_message)
        if m:
            removed = self.forget(workspace_id, m.group(1).strip())
            return ("forgot", "、".join(removed) if removed else "无匹配记忆")
        return None

    # ------------------------------------------------------------------
    # 检索（注入用）
    # ------------------------------------------------------------------

    def index_section(self, workspace_id: str) -> str:
        """MEMORY.md 索引内容（前 INDEX_MAX_LINES 行），无索引返回空串。"""
        try:
            with open(self._index_path(workspace_id), "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            return "\n".join(lines[:INDEX_MAX_LINES])
        except (OSError, ValueError):
            return ""

    def selected_files(self, workspace_id: str, query: str) -> list[str]:
        """关键词命中最多 MAX_LOAD_FILES 个记忆文件的完整内容。

        query 为空时取最近创建的 MAX_LOAD_FILES 个文件（按 mtime 降序）。
        """
        files = self._list_memory_files(workspace_id)
        if not files:
            return []
        query_lower = (query or "").lower()
        scored: list[tuple[int, str]] = []
        for fname in files:
            raw = self._read_memory_file(workspace_id, fname)
            score = sum(1 for token in query_lower.split() if token and token in raw.lower())
            if query_lower and any(t in raw.lower() for t in query_lower.split()):
                scored.append((score, fname))
        if query_lower:
            scored.sort(key=lambda x: x[0], reverse=True)
            picked = [fname for _, fname in scored[:MAX_LOAD_FILES]]
        else:
            # 无检索词：按修改时间取最近
            picked = sorted(
                files,
                key=lambda f: os.path.getmtime(os.path.join(self._ws_dir(workspace_id), f)),
                reverse=True,
            )[:MAX_LOAD_FILES]
        return [self._read_memory_file(workspace_id, f) for f in picked]

    def recall_section(self, workspace_id: str, query: str = "") -> str:
        """构造系统提示词里的记忆段落：索引 + 命中文件全文。无记忆返回空串。"""
        index = self.index_section(workspace_id)
        if not index:
            return ""
        parts = ["[经营长期记忆] 记忆索引（跨会话保留）：", index]
        selected = self.selected_files(workspace_id, query)
        for body in selected:
            parts.append("\n<记忆文件>\n" + body)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 索引维护
    # ------------------------------------------------------------------

    def refresh_index(self, workspace_id: str) -> None:
        """扫描记忆目录，重建 MEMORY.md 索引。"""
        lines = ["# 经营长期记忆索引", "# 格式：- [名称](文件) — 简述 (type: 类型)"]
        for fname in sorted(self._list_memory_files(workspace_id)):
            meta = self._read_frontmatter(workspace_id, fname)
            if not meta:
                continue
            lines.append(
                f"- [{meta.get('name', fname)}]({fname}) — {meta.get('description', '')} "
                f"(type: {meta.get('type', DEFAULT_TYPE)})"
            )
        os.makedirs(self._ws_dir(workspace_id), exist_ok=True)
        with open(self._index_path(workspace_id), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------------
    # 文件 I/O
    # ------------------------------------------------------------------

    def _write_memory_file(self, workspace_id: str, name: str, body: str) -> None:
        os.makedirs(self._ws_dir(workspace_id), exist_ok=True)
        with open(self._file_path(workspace_id, _slug(name)), "w", encoding="utf-8") as f:
            f.write(body)

    def _list_memory_files(self, workspace_id: str) -> list[str]:
        ws_dir = self._ws_dir(workspace_id)
        if not os.path.isdir(ws_dir):
            return []
        return [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]

    def _read_memory_file(self, workspace_id: str, fname: str) -> str:
        """读取指定记忆文件（fname 含 .md 后缀）。"""
        path = os.path.join(self._ws_dir(workspace_id), fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _read_frontmatter(self, workspace_id: str, fname: str) -> dict[str, str]:
        content = self._read_memory_file(workspace_id, fname)
        meta: dict[str, str] = {}
        if content.startswith("---"):
            block = content.split("---", 2)
            if len(block) >= 2:
                for line in block[1].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
        return meta

    def _name_exists(self, workspace_id: str, prefix: str) -> bool:
        return any(
            prefix in f or prefix in self._read_memory_file(workspace_id, f)
            for f in self._list_memory_files(workspace_id)
        )


# 全局默认记忆存储（目录 data/memory，可用 MEMORY_DIR 覆盖）
DEFAULT_MEMORY_STORE = MemoryStore()