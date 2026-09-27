"""Dataset、TestCase 与 EvaluationSuite 的不可变定义。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, JsonValue, freeze_json, json_compatible, require_text
from app.core.evaluation.references import (
    ArtifactRef,
    CapabilityRequirement,
    CaseVersionRef,
    EvidenceRef,
    VersionRef,
    freeze_metadata,
)


def _freeze_unique_strings(values: tuple[str, ...] | list[str], field_name: str) -> tuple[str, ...]:
    frozen = tuple(values)
    for value in frozen:
        require_text(value, field_name)
    if len(frozen) != len(set(frozen)):
        raise ValueError(f"duplicate {field_name} is not allowed")
    return frozen


def _reject_duplicates(values: tuple[object, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field_name} is not allowed")


@dataclass(frozen=True, slots=True)
class AssertionSpec:
    """不绑定具体 Assertion Engine 的最小断言描述。"""

    assertion_id: str
    kind: str
    config: FrozenJsonValue = field(default_factory=FrozenDict, compare=False)
    required: bool = True

    def __post_init__(self) -> None:
        require_text(self.assertion_id, "assertion_id")
        require_text(self.kind, "kind")
        object.__setattr__(self, "config", freeze_json(self.config))


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """拥有有序 Case collection 与 version lineage 的 Dataset snapshot。"""

    dataset_id: str
    version: str
    name: str = field(compare=False)
    created_at: datetime = field(compare=False)
    parent_version: str | None = field(default=None, compare=False)
    description: str | None = field(default=None, compare=False)
    case_version_refs: tuple[CaseVersionRef, ...] = field(default=(), compare=False)
    tags: tuple[str, ...] = field(default=(), compare=False)
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.dataset_id, "dataset_id")
        require_text(self.version, "version")
        require_text(self.name, "name")
        if self.parent_version is not None:
            require_text(self.parent_version, "parent_version")
            if self.parent_version == self.version:
                raise ValueError("parent_version must differ from version")
        refs = tuple(self.case_version_refs)
        _reject_duplicates(refs, "case_version_ref")
        object.__setattr__(self, "case_version_refs", refs)
        object.__setattr__(self, "tags", _freeze_unique_strings(self.tags, "tag"))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TestCaseVersion:
    """与具体 Agent runtime 和 Trace 解耦的 TestCase snapshot。"""

    case_id: str
    version: str
    name: str = field(compare=False)
    input_payload: FrozenJsonValue = field(compare=False)
    created_at: datetime = field(compare=False)
    expected_output: FrozenJsonValue | None = field(default=None, compare=False)
    assertion_specs: tuple[AssertionSpec, ...] = field(default=(), compare=False)
    fixture_refs: tuple[ArtifactRef, ...] = field(default=(), compare=False)
    evidence_refs: tuple[EvidenceRef, ...] = field(default=(), compare=False)
    tags: tuple[str, ...] = field(default=(), compare=False)
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.version, "version")
        require_text(self.name, "name")
        object.__setattr__(self, "input_payload", freeze_json(self.input_payload))
        if self.expected_output is not None:
            object.__setattr__(self, "expected_output", freeze_json(self.expected_output))
        assertions = tuple(self.assertion_specs)
        _reject_duplicates(tuple(item.assertion_id for item in assertions), "assertion_id")
        object.__setattr__(self, "assertion_specs", assertions)
        object.__setattr__(self, "fixture_refs", tuple(self.fixture_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "tags", _freeze_unique_strings(self.tags, "tag"))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))

    @property
    def semantic_digest(self) -> str:
        """Evaluator 可消费内容的稳定 SHA-256 identity。"""
        semantic = {
            "input_payload": json_compatible(self.input_payload),
            "expected_output": json_compatible(self.expected_output),
            "assertion_specs": [
                {"assertion_id": item.assertion_id, "kind": item.kind,
                 "config": json_compatible(item.config), "required": item.required}
                for item in self.assertion_specs
            ],
            "fixture_refs": [
                {"artifact_id": item.artifact_id, "digest": item.digest,
                 "media_type": item.media_type, "metadata": json_compatible(item.metadata)}
                for item in self.fixture_refs
            ],
            "evidence_refs": [
                {"kind": item.kind, "identifier": item.identifier, "media_type": item.media_type,
                 "schema_version": item.schema_version, "metadata": json_compatible(item.metadata)}
                for item in self.evidence_refs
            ],
            "metadata": json_compatible(self.metadata),
        }
        encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EvaluatorKind(StrEnum):
    """Evaluator 的最小稳定类别。"""

    DETERMINISTIC = "DETERMINISTIC"
    LLM_JUDGE = "LLM_JUDGE"


class ScoreDirection(StrEnum):
    """分数在后续比较中的方向语义。"""

    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class EvaluatorSpec:
    """Suite 中 evaluator 实际运行配置的权威 snapshot。"""

    evaluator_id: str
    evaluator_version: str
    evaluator_kind: EvaluatorKind = field(compare=False)
    config_ref: VersionRef = field(compare=False)
    score_direction: ScoreDirection = field(compare=False)
    config_snapshot: FrozenJsonValue = field(default_factory=FrozenDict, compare=False)
    threshold: float | None = field(default=None, compare=False)
    score_range: tuple[float, float] | None = field(default=None, compare=False)
    comparison_tolerance: float | None = field(default=None, compare=False)
    prompt_ref: VersionRef | None = field(default=None, compare=False)
    required: bool = field(default=True, compare=False)
    result_schema_ref: VersionRef = field(default=VersionRef("evaluation_result", "v1"), compare=False)
    comparison_semantics: str = field(default="verdict_transition.v1", compare=False)
    required_artifact_kinds: tuple[str, ...] = field(default=(), compare=False)
    required_evidence_kinds: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        require_text(self.evaluator_id, "evaluator_id")
        if not isinstance(self.result_schema_ref, VersionRef):
            raise TypeError("result_schema_ref must be VersionRef")
        require_text(self.comparison_semantics, "comparison_semantics")
        object.__setattr__(self, "required_artifact_kinds", _freeze_unique_strings(
            self.required_artifact_kinds, "required_artifact_kind"))
        object.__setattr__(self, "required_evidence_kinds", _freeze_unique_strings(
            self.required_evidence_kinds, "required_evidence_kind"))
        require_text(self.evaluator_version, "evaluator_version")
        if not isinstance(self.evaluator_kind, EvaluatorKind):
            raise ValueError("unknown evaluator_kind")
        if not isinstance(self.score_direction, ScoreDirection):
            raise ValueError("unknown score_direction")
        if not isinstance(self.config_ref, VersionRef):
            raise TypeError("config_ref must be VersionRef")
        object.__setattr__(self, "config_snapshot", freeze_json(self.config_snapshot))
        if self.threshold is not None and not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite")
        if self.score_range is not None:
            score_range = tuple(self.score_range)
            if len(score_range) != 2 or not all(math.isfinite(value) for value in score_range):
                raise ValueError("score_range must contain two finite values")
            if score_range[0] > score_range[1]:
                raise ValueError("score_range minimum must not exceed maximum")
            object.__setattr__(self, "score_range", score_range)
        if self.comparison_tolerance is not None:
            if not math.isfinite(self.comparison_tolerance) or self.comparison_tolerance < 0:
                raise ValueError("comparison_tolerance must be finite and non-negative")

    @property
    def definition_digest(self) -> str:
        """评分规则与输出语义字段的稳定 SHA-256 identity。"""
        semantic = {
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evaluator_kind": self.evaluator_kind.value,
            "config_ref": {"kind": self.config_ref.kind, "opaque_value": self.config_ref.opaque_value},
            "config_snapshot": json_compatible(self.config_snapshot),
            "threshold": self.threshold,
            "score_direction": self.score_direction.value,
            "score_range": self.score_range,
            "comparison_tolerance": self.comparison_tolerance,
            "prompt_ref": None if self.prompt_ref is None else {
                "kind": self.prompt_ref.kind, "opaque_value": self.prompt_ref.opaque_value
            },
            "required": self.required,
            "result_schema_ref": {"kind": self.result_schema_ref.kind, "opaque_value": self.result_schema_ref.opaque_value},
            "comparison_semantics": self.comparison_semantics,
            "required_artifact_kinds": self.required_artifact_kinds,
            "required_evidence_kinds": self.required_evidence_kinds,
        }
        encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PolicyDisposition(StrEnum):
    """Suite 对非正常 evaluator 状态的上层处理方式。"""

    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    """Phase 2 所需的最小评价策略 snapshot。"""

    required_result_missing: PolicyDisposition = PolicyDisposition.INCONCLUSIVE
    evaluator_error: PolicyDisposition = PolicyDisposition.INCONCLUSIVE
    evaluator_inconclusive: PolicyDisposition = PolicyDisposition.INCONCLUSIVE
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        for field_name in ("required_result_missing", "evaluator_error", "evaluator_inconclusive"):
            if not isinstance(getattr(self, field_name), PolicyDisposition):
                raise ValueError(f"unknown {field_name} disposition")
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class EvaluationSuiteVersion:
    """Case selection、EvaluatorSpec 和 Policy 的不可变 Suite snapshot。"""

    suite_id: str
    version: str
    case_selection: tuple[CaseVersionRef, ...] = field(compare=False)
    evaluator_specs: tuple[EvaluatorSpec, ...] = field(compare=False)
    evaluation_policy: EvaluationPolicy = field(compare=False)
    created_at: datetime = field(compare=False)
    target_capability_requirements: tuple[CapabilityRequirement, ...] = field(default=(), compare=False)
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.suite_id, "suite_id")
        require_text(self.version, "version")
        cases = tuple(self.case_selection)
        evaluators = tuple(self.evaluator_specs)
        requirements = tuple(self.target_capability_requirements)
        _reject_duplicates(cases, "selected case")
        identities = tuple((item.evaluator_id, item.evaluator_version) for item in evaluators)
        _reject_duplicates(identities, "evaluator identity/version")
        _reject_duplicates(requirements, "target capability requirement")
        object.__setattr__(self, "case_selection", cases)
        object.__setattr__(self, "evaluator_specs", evaluators)
        object.__setattr__(self, "target_capability_requirements", requirements)
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
