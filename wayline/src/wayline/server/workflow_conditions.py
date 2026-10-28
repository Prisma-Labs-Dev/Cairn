"""Bounded workflow outcome contracts and predicates. No executable expressions."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator, field_validator

NAME = r"[A-Za-z][A-Za-z0-9_-]{0,63}"


def scalar_type(value: Any) -> str:
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "number"
    if type(value) is str and len(value) <= 2048:
        return "string"
    raise ValueError("results require finite scalar values; strings allow at most 2048 characters")


class ResultField(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["integer", "number", "boolean", "string"] | None = None
    enum: list[Any] | None = Field(default=None, min_length=1, max_length=64)
    minimum: int | float | None = None
    maximum: int | float | None = None

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def strict_bound(cls, value):
        if value is not None and type(value) not in (int, float):
            raise ValueError("bounds must be numbers")
        return value

    @model_validator(mode="after")
    def check_contract(self):
        if self.type is None:
            if self.enum is None:
                raise ValueError("a result field needs type or enum")
            kinds = {scalar_type(value) for value in self.enum}
            if kinds <= {"integer", "number"}:
                self.type = "number" if "number" in kinds else "integer"
            elif len(kinds) == 1:
                self.type = kinds.pop()
            else:
                raise ValueError("enum values must have one scalar type")
        for bound in (self.minimum, self.maximum):
            if bound is not None and (type(bound) not in (int, float) or (type(bound) is float and not math.isfinite(bound))):
                raise ValueError("bounds must be finite numbers")
        if self.type not in ("integer", "number") and (self.minimum is not None or self.maximum is not None):
            raise ValueError("bounds require a numeric result")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        if self.enum is not None:
            seen = set()
            for value in self.enum:
                self.validate_value(value, check_enum=False)
                key = json.dumps(value)
                if key in seen:
                    raise ValueError("enum values must be unique")
                seen.add(key)
        return self

    def validate_value(self, value, *, check_enum=True):
        kind = scalar_type(value)
        if kind != self.type and not (self.type == "number" and kind == "integer"):
            raise ValueError(f"expected {self.type}")
        if self.minimum is not None and value < self.minimum:
            raise ValueError("value is below minimum")
        if self.maximum is not None and value > self.maximum:
            raise ValueError("value exceeds maximum")
        if check_enum and self.enum is not None and value not in self.enum:
            raise ValueError("value is not in enum")


def validate_contract(contract):
    if len(contract) > 32 or any(not re.fullmatch(NAME, name) for name in contract):
        raise ValueError("results allow at most 32 named fields")
    if len(json.dumps({name: field.model_dump(exclude_none=True) for name, field in contract.items()}).encode("utf-8")) > 16384:
        raise ValueError("result contract exceeds 16384 bytes")
    return contract


def validate_result(contract, result):
    if not contract:
        if result not in (None, {}):
            raise ValueError("this stage does not declare a result contract")
        return
    if not isinstance(result, dict) or set(result) != set(contract):
        raise ValueError("result must contain exactly the declared fields")
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > 16384:
        raise ValueError("result exceeds 16384 bytes")
    for name, field in contract.items():
        try:
            field.validate_value(result[name])
        except ValueError as exc:
            raise ValueError(f"result.{name}: {exc}") from exc


class StageDependency(BaseModel):
    model_config = {"extra": "forbid"}
    stage: str = Field(pattern=f"^{NAME}$")
    when: str | None = Field(default=None, min_length=1, max_length=512)
    allow_not_applicable: bool = Field(default=False, strict=True)


class CollectionDependency(BaseModel):
    model_config = {"extra": "forbid"}
    collection: str = Field(pattern=f"^{NAME}$")
    allow_not_applicable: bool = Field(default=False, strict=True)


class CollectionEmission(BaseModel):
    model_config = {"extra": "forbid"}
    collection: str = Field(pattern=f"^{NAME}$")
    result: dict[str, ResultField] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_result(self):
        validate_contract(self.result)
        return self


def parse_predicate(text):
    match = re.fullmatch(rf"({NAME})\.result\.({NAME})\s+(==|!=|>=|<=|>|<|in)\s+(.+)", text)
    if not match:
        raise ValueError("condition must be 'stage.result.field operator JSON-literal'")
    stage, field, op, encoded = match.groups()
    try:
        literal = json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite literal")))
    except (ValueError, RecursionError) as exc:
        raise ValueError("condition requires a JSON scalar or membership list") from exc
    values = literal if op == "in" else [literal]
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("membership requires a nonempty list of at most 64 scalar values")
    for value in values:
        scalar_type(value)
    return stage, field, op, literal


def validate_predicate(dependency, definitions):
    stage, name, op, literal = parse_predicate(dependency.when)
    if stage != dependency.stage:
        raise ValueError("condition must reference its dependency stage")
    contract = definitions[stage].result
    if name not in contract:
        raise ValueError("condition references an undeclared result field")
    field = contract[name]
    if op in (">", ">=", "<", "<=") and field.type not in ("integer", "number"):
        raise ValueError("ordered comparisons require numeric result fields")
    values = literal if op == "in" else [literal]
    for value in values:
        kind = scalar_type(value)
        if kind != field.type and not (kind == "integer" and field.type == "number"):
            raise ValueError("condition literal has the wrong type")
    if field.enum is not None:
        if not any(evaluate_predicate(dependency.when, {name: candidate}) for candidate in field.enum):
            raise ValueError("condition cannot match the declared enum")
    if op in ("==", "in"):
        def in_bounds(value):
            return (field.minimum is None or value >= field.minimum) and (field.maximum is None or value <= field.maximum)
        if not any(in_bounds(value) for value in values):
            raise ValueError("condition cannot match the declared bounds")
    if ((op == ">" and field.maximum is not None and literal >= field.maximum)
            or (op == ">=" and field.maximum is not None and literal > field.maximum)
            or (op == "<" and field.minimum is not None and literal <= field.minimum)
            or (op == "<=" and field.minimum is not None and literal < field.minimum)):
        raise ValueError("condition cannot match the declared bounds")


def evaluate_predicate(text, result):
    _, field, op, literal = parse_predicate(text)
    value = result[field]
    if op == "==": return value == literal
    if op == "!=": return value != literal
    if op == ">": return value > literal
    if op == ">=": return value >= literal
    if op == "<": return value < literal
    if op == "<=": return value <= literal
    return value in literal


def dependency_stage(dependency, collections):
    if isinstance(dependency, str): return dependency
    if isinstance(dependency, StageDependency): return dependency.stage
    return collections[dependency.collection]
