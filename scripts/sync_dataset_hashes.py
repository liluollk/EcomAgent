"""同步 ``manifest.json`` 里的数据集哈希(数据集改动后必须执行)。"""

import hashlib
import re
from pathlib import Path


DATASET_DIR = Path("eval/datasets/product_operations")
MANIFEST = DATASET_DIR / "manifest.json"

text = MANIFEST.read_text(encoding="utf-8")
for split in ("core", "blind"):
    digest = hashlib.sha256((DATASET_DIR / f"{split}.yaml").read_bytes()).hexdigest()
    text = re.sub(rf'"{split}_sha256": "[0-9a-f]{{64}}"', f'"{split}_sha256": "{digest}"', text)
    print(split, digest[:16])
MANIFEST.write_text(text, encoding="utf-8")
