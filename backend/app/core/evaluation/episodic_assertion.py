"""WP6-E Episodic Layer1 evaluation 的 assertion 代数、冻结 failure taxonomy 与 blocked 语义。

本模块是 Episodic evaluation 的 assertion 权威，与 WP5 ``stateful_assertion`` 保持
概念一致但独立：WP5 的 ``FailureTaxonomy`` 与 ``BlockReason`` 属于 semantic-memory
domain，不能被当作 Episodic taxonomy 使用。``AssertionStatus`` 直接复用
``stateful_assertion.AssertionStatus``（PASS / FAIL / BLOCKED / NOT_APPLICABLE）。

冻结语义：

- ``PASS`` = evidence sufficient + expectation satisfied。
- ``FAIL`` = evidence sufficient + expectation violated（必须携带 failure_taxonomy）。
- ``BLOCKED`` = cannot fairly evaluate（必须携带 blocked_by）。
- ``NOT_APPLICABLE`` = Dataset 明确不适用。

缺 evidence 一律不得映射为 FAIL 或 PASS；identity evidence missing 是
``BLOCKED / EVIDENCE_CAPTURE``，与“identity exists but Runtime didn't select”的
``FAIL`` 严格区分。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.evaluation.immutable import FrozenDict, JsonValue, freeze_json, require_text
from app.core.evaluation.stateful_assertion import AssertionStatus


class EpisodicAssertionGroup(StrEnum):
    """Dataset 冻结的 13 类 typed assertion group。"""

    FORMATION = "FORMATION"
    ELIGIBILITY = "ELIGIBILITY"
    EPISODE_STRUCTURE = "EPISODE_STRUCTURE"
    EVIDENCE_GROUNDING = "EVIDENCE_GROUNDING"
    PRIVACY = "PRIVACY"
    PERSISTENCE = "PERSISTENCE"
    IDEMPOTENCY = "IDEMPOTENCY"
    RETRIEVAL = "RETRIEVAL"
    RANKING = "RANKING"
    SCOPE_ISOLATION = "SCOPE_ISOLATION"
    INJECTION = "INJECTION"
    TRUST_BOUNDARY = "TRUST_BOUNDARY"
    INVARIANT = "INVARIANT"


class EpisodicFailureTaxonomy(StrEnum):
    """冻结 failure taxonomy（WP6-E Architecture §39；禁止每 Scenario 自造 code）。

    ``RUNTIME_BEHAVIORAL_FAILURE`` 表示真实 Runtime 行为违反 Ground Truth；
    ``EVALUATION_INFRA_FAILURE`` 表示 provision/transport/journal/projection/artifact
    /serializer 等 evaluation-infra 失败；二者必须分开记录。
    """

    EPISODE_FORMATION_FALSE_NEGATIVE = "EPISODE_FORMATION_FALSE_NEGATIVE"
    EPISODE_FORMATION_FALSE_POSITIVE = "EPISODE_FORMATION_FALSE_POSITIVE"
    EPISODE_GROUNDING_MISMATCH = "EPISODE_GROUNDING_MISMATCH"
    EPISODE_FABRICATED_FACT = "EPISODE_FABRICATED_FACT"
    EPISODE_PRIVACY_VIOLATION = "EPISODE_PRIVACY_VIOLATION"
    EPISODE_IDEMPOTENCY_VIOLATION = "EPISODE_IDEMPOTENCY_VIOLATION"
    EPISODE_RETRIEVAL_MISS = "EPISODE_RETRIEVAL_MISS"
    EPISODE_IRRELEVANT_SELECTION = "EPISODE_IRRELEVANT_SELECTION"
    EPISODE_SCOPE_LEAKAGE = "EPISODE_SCOPE_LEAKAGE"
    EPISODE_CONTEXT_INJECTION_MISS = "EPISODE_CONTEXT_INJECTION_MISS"
    EPISODE_INSTRUCTION_ELEVATION = "EPISODE_INSTRUCTION_ELEVATION"
    EPISODE_INVARIANT_VIOLATION = "EPISODE_INVARIANT_VIOLATION"
    RUNTIME_BEHAVIORAL_FAILURE = "RUNTIME_BEHAVIORAL_FAILURE"
    EVALUATION_INFRA_FAILURE = "EVALUATION_INFRA_FAILURE"
    EXPECTED_EVIDENCE_LIMITATION = "EXPECTED_EVIDENCE_LIMITATION"


class EpisodicBlockReason(StrEnum):
    """BLOCKED 的必须分类（与 WP5 分离；identity evidence missing 属于 EVIDENCE_CAPTURE）。"""

    RUNTIME_BLOCKED = "RUNTIME_BLOCKED"
    EVIDENCE_CAPTURE = "EVIDENCE_CAPTURE"
    EXPECTED_EVIDENCE_LIMITATION = "EXPECTED_EVIDENCE_LIMITATION"
    PREREQUISITE = "PREREQUISITE"
    EVALUATION_INFRA = "EVALUATION_INFRA"


EVALUABLE_STATUSES: Final[frozenset[str]] = frozenset({AssertionStatus.PASS.value, AssertionStatus.FAIL.value})

#: EVIDENCE_GROUNDING 断言拆分为两类独立判断（68 Gate frozen contract）：
#: runtime identity grounding（Dataset symbolic step_ref -> canonical Journal
#: RuntimeEvent.step_id/status）与 persisted observation fidelity（human-readable
#: observation/result 真实性）。``assertion_id`` 已含 ``.grounding`` 前缀。
RUNTIME_IDENTITY_GROUNDING_ASSERTION: Final[str] = ".identity"
PERSISTED_OBSERVATION_FIDELITY_ASSERTION: Final[str] = ".fidelity"


@dataclass(frozen=True, slots=True)
class EpisodicAssertion:
    """一个可追溯的 Episodic assertion result。

    - expected / actual_evidence：JSON-safe 投影或 evidence identifier（不复制
      canonical_text / situation / lesson 正文）。
    - failure_taxonomy：仅 FAIL 设置 primary taxonomy。
    - blocked_by：仅 BLOCKED 设置。
    - evidence_source：实际证据来源标识（如 ``capture`` / ``formation_receipt`` /
      ``fixture_receipt`` / ``replay_receipt`` / ``runtime_receipt`` / ``journal`` /
      ``sqlite_projection`` / ``runtime``）。
    """

    assertion_id: str
    group: EpisodicAssertionGroup
    status: AssertionStatus
    expected: JsonValue = None
    actual_evidence: JsonValue = None
    failure_taxonomy: EpisodicFailureTaxonomy | None = None
    blocked_by: EpisodicBlockReason | None = None
    evidence_source: str | None = None
    reason: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        require_text(self.assertion_id, "assertion_id")
        if not isinstance(self.group, EpisodicAssertionGroup):
            raise ValueError("unknown assertion group")
        if not isinstance(self.status, AssertionStatus):
            raise ValueError("unknown assertion status")
        if self.failure_taxonomy is not None and not isinstance(self.failure_taxonomy, EpisodicFailureTaxonomy):
            raise TypeError("failure_taxonomy must be EpisodicFailureTaxonomy")
        if self.blocked_by is not None and not isinstance(self.blocked_by, EpisodicBlockReason):
            raise TypeError("blocked_by must be EpisodicBlockReason")
        object.__setattr__(self, "expected", freeze_json(self.expected))
        object.__setattr__(self, "actual_evidence", freeze_json(self.actual_evidence))

        if self.status is AssertionStatus.FAIL:
            if self.failure_taxonomy is None:
                raise ValueError("FAIL assertion requires a failure_taxonomy")
        elif self.failure_taxonomy is not None:
            raise ValueError("non-FAIL assertion must not declare failure_taxonomy")

        if self.status is AssertionStatus.BLOCKED:
            if self.blocked_by is None:
                raise ValueError("BLOCKED assertion requires a blocked_by reason")
        elif self.blocked_by is not None:
            raise ValueError("non-BLOCKED assertion must not declare blocked_by")

        if self.status is AssertionStatus.PASS and self.reason is None:
            raise ValueError("PASS assertion requires a reason")

    def to_metadata(self) -> FrozenDict:
        """序列化为 JSON-safe provenance 快照。"""
        return freeze_json(
            {
                "assertion_id": self.assertion_id,
                "group": self.group.value,
                "status": self.status.value,
                "expected": self.expected,
                "actual_evidence": self.actual_evidence,
                "failure_taxonomy": (self.failure_taxonomy.value if self.failure_taxonomy is not None else None),
                "blocked_by": self.blocked_by.value if self.blocked_by is not None else None,
                "evidence_source": self.evidence_source,
                "reason": self.reason,
                "required": self.required,
            }
        )


def status_counts(assertions: list[EpisodicAssertion]) -> dict[str, int]:
    """统计全部 assertion 的 status 计数。"""
    counts: dict[str, int] = {
        AssertionStatus.PASS.value: 0,
        AssertionStatus.FAIL.value: 0,
        AssertionStatus.BLOCKED.value: 0,
        AssertionStatus.NOT_APPLICABLE.value: 0,
    }
    for item in assertions:
        counts[item.status.value] += 1
    return counts


def required_assertions(assertions: list[EpisodicAssertion]) -> list[EpisodicAssertion]:
    """返回 required assertions。"""
    return [item for item in assertions if item.required]


def scenario_success_assertion(
    assertion_id: str,
    *,
    required_pass_count: int,
    required_fail_count: int,
    required_blocked_count: int,
    optional_fail_count: int,
    failure_taxonomies: tuple[EpisodicFailureTaxonomy, ...] = (),
) -> EpisodicAssertion:
    """Scenario PASS 规则：所有 required applicable assertion PASS，无 FAIL/BLOCKED。

    P0 violation 直接使 Scenario FAIL。BLOCKED 使 Scenario BLOCKED（reason=
    PREREQUISITE），FAIL 使 Scenario FAIL。optional failure 不阻塞 scenario。
    """
    if required_fail_count > 0:
        return EpisodicAssertion(
            assertion_id=assertion_id,
            group=EpisodicAssertionGroup.INVARIANT,
            status=AssertionStatus.FAIL,
            expected={"required_pass": required_pass_count, "required_fail": 0},
            actual_evidence={
                "required_fail": required_fail_count,
                "required_blocked": required_blocked_count,
                "optional_fail": optional_fail_count,
                "failure_taxonomies": sorted({item.value for item in failure_taxonomies}),
            },
            failure_taxonomy=EpisodicFailureTaxonomy.RUNTIME_BEHAVIORAL_FAILURE,
            reason="required applicable assertion failed",
        )
    if required_blocked_count > 0:
        return EpisodicAssertion(
            assertion_id=assertion_id,
            group=EpisodicAssertionGroup.INVARIANT,
            status=AssertionStatus.BLOCKED,
            expected={"required_pass": required_pass_count, "required_blocked": 0},
            actual_evidence={
                "required_fail": required_fail_count,
                "required_blocked": required_blocked_count,
                "optional_fail": optional_fail_count,
            },
            blocked_by=EpisodicBlockReason.PREREQUISITE,
            reason="required applicable assertion is blocked",
        )
    return EpisodicAssertion(
        assertion_id=assertion_id,
        group=EpisodicAssertionGroup.INVARIANT,
        status=AssertionStatus.PASS,
        expected={"required_pass": required_pass_count},
        actual_evidence={
            "required_fail": 0,
            "required_blocked": 0,
            "optional_fail": optional_fail_count,
        },
        reason="all required applicable assertions passed",
    )


__all__ = [
    "EVALUABLE_STATUSES",
    "PERSISTED_OBSERVATION_FIDELITY_ASSERTION",
    "RUNTIME_IDENTITY_GROUNDING_ASSERTION",
    "EpisodicAssertion",
    "EpisodicAssertionGroup",
    "EpisodicBlockReason",
    "EpisodicFailureTaxonomy",
    "required_assertions",
    "scenario_success_assertion",
    "status_counts",
]
