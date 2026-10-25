"""同步 ``manifest.json`` 里的数据集哈希(数据集改动后必须执行)。

冻结哈希按**文件字节**计算,所以字节必须与检出一致:``.gitattributes`` 声明
``eol=lf``,Linux/CI 检出的是 LF;若本地在 Windows 上用 ``Path.write_text``
改过数据集,Python 会把 ``\\n`` 翻成 ``\\r\\n``,哈希跟着变成 CRLF 的值,CI 上
``test_frozen_hashes_match_file_bytes`` 必然失败(2026-10-25 就撞到这一次)。

因此这里先把文件归一成 LF 再算哈希,manifest 也以 LF 写出。
"""

import hashlib
import re
from pathlib import Path


DATASET_DIR = Path("eval/datasets/product_operations")
MANIFEST = DATASET_DIR / "manifest.json"


def _read_lf(path: Path) -> bytes:
    """读成 LF 字节;本地是 CRLF 时顺手落盘归一,保证字节与哈希同源。"""
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    if normalized != raw:
        path.write_bytes(normalized)
        print(f"{path.name}: 归一为 LF ({len(raw)} → {len(normalized)} bytes)")
    return normalized


text = _read_lf(MANIFEST).decode("utf-8")
for split in ("core", "blind"):
    digest = hashlib.sha256(_read_lf(DATASET_DIR / f"{split}.yaml")).hexdigest()
    text = re.sub(rf'"{split}_sha256": "[0-9a-f]{{64}}"', f'"{split}_sha256": "{digest}"', text)
    print(split, digest[:16])
MANIFEST.write_bytes(text.encode("utf-8"))
