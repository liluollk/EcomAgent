"""Runtime Schema Validator(计划 02 Task 6;架构文档 §9)。

基于 jsonschema,对进入管线的 ToolCall 参数完成 JSON 解析、根结构校验和
针对已解析 ToolVersion ``input_schema`` 的类型/结构/未允许字段校验。校验
错误不等价处理(架构文档 §9):

- 参数不可解析 JSON、根结构错误(非 JSON 对象)或出现未允许字段,属于模型
  输出质量问题 => ``INVALID_MODEL_OUTPUT``,不向用户询问技术性问题;
- 必填业务字段缺失 => 生成 ``SchemaDiagnostic(field_paths)``,交由后续
  InputRequirementResolver 阶段决定能否转换为用户可回答的 Input Request;
- 其余结构违规(类型错误等)按模型输出质量问题处理。
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict

from runtime.execution.pretool.decisions import SchemaDiagnostic


SchemaValidationStatus = Literal["VALID", "INVALID_MODEL_OUTPUT", "MISSING_FIELDS"]


class SchemaValidationOutcome(BaseModel):
    """Schema 校验结果值对象;三种状态互斥携带不同字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SchemaValidationStatus
    arguments: Mapping[str, Any] | None = None
    diagnostic: SchemaDiagnostic | None = None
    message: str | None = None


class RuntimeSchemaValidator:
    """使用已解析 ToolVersion 的 JSON Schema 校验原始参数(无状态)。"""

    def validate(
        self, *, raw_arguments: str, input_schema: Mapping[str, Any]
    ) -> SchemaValidationOutcome:
        """解析并校验参数;结果为 VALID / INVALID_MODEL_OUTPUT / MISSING_FIELDS。"""
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            return SchemaValidationOutcome(
                status="INVALID_MODEL_OUTPUT",
                message=f"arguments are not parseable JSON: {error.msg}",
            )
        if not isinstance(parsed, dict):
            return SchemaValidationOutcome(
                status="INVALID_MODEL_OUTPUT",
                message="arguments root structure must be a JSON object",
            )

        schema = dict(input_schema)
        validator = validator_for(schema)(schema)
        errors = sorted(
            validator.iter_errors(parsed),
            key=lambda validation_error: [
                str(part) for part in validation_error.absolute_path
            ],
        )

        missing_paths: list[str] = []
        for validation_error in errors:
            if validation_error.validator == "required":
                missing_paths.extend(_missing_field_paths(validation_error))
                continue
            # 其余违规(未允许字段、类型错误等)均为模型输出质量问题。
            return SchemaValidationOutcome(
                status="INVALID_MODEL_OUTPUT",
                message=validation_error.message,
            )

        if missing_paths:
            diagnostic = SchemaDiagnostic(
                field_paths=tuple(dict.fromkeys(missing_paths)),
                message="required business fields are missing",
            )
            return SchemaValidationOutcome(
                status="MISSING_FIELDS",
                arguments=parsed,
                diagnostic=diagnostic,
                message=diagnostic.message,
            )
        return SchemaValidationOutcome(status="VALID", arguments=parsed)


def _missing_field_paths(error: ValidationError) -> list[str]:
    """从 ``required`` 校验错误提取缺失字段路径(含嵌套路径,点号连接)。"""
    required = error.validator_value
    instance = error.instance
    if not isinstance(required, list) or not isinstance(instance, dict):
        return []
    prefix = [str(part) for part in error.absolute_path]
    return [
        ".".join([*prefix, str(name)]) for name in required if name not in instance
    ]


__all__ = [
    "RuntimeSchemaValidator",
    "SchemaValidationOutcome",
    "SchemaValidationStatus",
]
