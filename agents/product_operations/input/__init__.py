"""商品域参数归一化与 Provenance(计划 03 Task 3;架构文档 §5)。

- ``normalizer``:确定性归一化(字段白名单、trim、金额 Decimal 化);
- ``provenance``:ArgumentProvenance 枚举与确定性证据规则(证据来源白名单);
- ``requirements``:商品域 InputRequirementResolver 与 runtime PreToolUse
  注入协议(计划 02 Task 6)的适配。
"""

from agents.product_operations.input.normalizer import (
    normalize_amount,
    normalize_arguments,
    normalize_label,
)
from agents.product_operations.input.provenance import (
    TRUSTED_PROVENANCE,
    ArgumentProvenance,
    EvidenceResolver,
    ProvenanceOutcome,
    StoreCandidate,
)
from agents.product_operations.input.requirements import (
    InputOption,
    ProductInputRequirementResolver,
    ProductInputResolution,
    ProductPreToolInputRequirementResolver,
    ProductPreToolNormalizer,
    ProductPreToolProvenanceResolver,
    StoreRequirement,
)


__all__ = [
    "TRUSTED_PROVENANCE",
    "ArgumentProvenance",
    "EvidenceResolver",
    "InputOption",
    "ProductInputRequirementResolver",
    "ProductInputResolution",
    "ProductPreToolInputRequirementResolver",
    "ProductPreToolNormalizer",
    "ProductPreToolProvenanceResolver",
    "ProvenanceOutcome",
    "StoreCandidate",
    "StoreRequirement",
    "normalize_amount",
    "normalize_arguments",
    "normalize_label",
]
