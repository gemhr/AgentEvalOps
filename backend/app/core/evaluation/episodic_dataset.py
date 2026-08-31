"""Stateful Episodic Evaluation Dataset v1 —— ``stateful-episodic-scenario.v1`` typed contract。

本模块是 WP6-E 的 Dataset Foundation 权威实现。它只定义 evaluation-only 的
Ground Truth 合同、严格 loader/validator 与 12-scenario inventory 约束；不实现
sequential runner、LocalAgent provisioner、capture adapter、journal collector、
final SQLite projector、evaluator、metrics、gate、baseline 或 aggregate artifact。

Dataset Authority：

- Dataset 是 Ground Truth Owner：描述期望 Runtime（LocalAgent）做什么。
- AgentEvalOps 是 Evaluation Owner：只能 provision / execute / read-only collect /
  evaluate / baseline，不得直接写 LocalAgent Memory 制造正确结果。
- LocalAgent 是 Runtime / Episode Owner：每个 Episode 必须由真实 Run formation 或
  （仅 E09 Layer1 fixture scenario）dataset-controlled initial fixture 形成。

设计约定（沿用既有 evaluation dataset coding style）：

- strict typed top-level contract；``extra="forbid"`` 拒绝 unknown field。
- ``dataset_schema_version`` 固定 ``stateful-episodic-scenario.v1``；
  ``dataset_id`` 固定 ``stateful_episodic_v1``；``version`` 固定 ``v1``。
- 12 个冻结 Scenario（E01..E12），``case_code`` 是稳定 Case identity，
  ``scenario_id`` 是 descriptive 稳定 ID，两者同时保留。
- 13 组 typed assertion contract：FORMATION / ELIGIBILITY / EPISODE_STRUCTURE /
  EVIDENCE_GROUNDING / PRIVACY / PERSISTENCE / IDEMPOTENCY / RETRIEVAL / RANKING /
  SCOPE_ISOLATION / INJECTION / TRUST_BOUNDARY / INVARIANT。run-scoped groups 位于
  ``EpisodicRun``，scenario-wide groups 位于 ``EpisodicScenarioAssertionGroups``。
- Symbolic Episode identity：Dataset 使用稳定 ``episode_ref``（如 ``run_a_episode``、
  ``foreign_scope_episode``），经 ``EpisodicEpisodeBinding`` 的 ``origin_run_id`` /
  fixture 关系映射真实 Episode；禁止 hardcode runtime-generated memory UUID。
- 不做任何 LLM judge expected score 的 hard Ground Truth；``usefulness_policy`` 固定
  ``OBSERVATIONAL_ONLY``。
- 只允许 fake privacy fixtures（``FAKE_`` 前缀 sentinel），拒绝真实 secret 形态。
- Layer2 identity 固定 ``EXPECTED_LIMITATION``，Layer1 固定 ``REQUIRED``。
- Evaluation control 与 LocalAgent evaluation-execute v3 的
  ``episodic-evaluation-control.v1`` 对齐；capability wire value、schema version 与
  legal capability composition 都以 target 真实源码为 authority。Dataset 不发送
  arbitrary plan / plan JSON / step list / tool / prompt / model output / callable /
  status override。
- E08 Run A 的 ``metadata.deterministic_plan_goal`` 只是 human-audit 用
  **descriptive metadata**，明确 ``NOT_TARGET_EXECUTION_AUTHORITY``；真正 execution
  authority 是 ``DETERMINISTIC_EPISODIC_SUCCESS_RUN`` capability + target-owned
  allowlisted ``DETERMINISTIC_EPISODIC_SUCCESS_V1`` profile。Runner 不得根据该文本
  动态构造 Plan。

本模块不修改 WP5 ``stateful_memory_v2``；共享 loader 仅新增 additive schema constant，
历史 Dataset bytes/digest 保持不变。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_EPISODIC_SCENARIO_SCHEMA_VERSION,
    EvaluationDatasetLoadError,
    _require_wire_id,
)
from app.core.evaluation.stateful_memory_dataset import content_digest
from app.core.evaluation.stateful_memory_dataset_v2 import (
    IdentityEvidenceByLayer,
    IdentityEvidenceRequirement,
)

EPISODIC_DATASET_ID: str = "stateful_episodic_v1"
EPISODIC_DATASET_VERSION: str = "v1"
EPISODIC_DATASET_V2_ID: str = "stateful_episodic_v2"
EPISODIC_DATASET_V2_VERSION: str = "v2"
EPISODIC_DATASET_SCHEMA_V1: str = "stateful-episodic-scenario.v1"
EPISODIC_DATASET_SCHEMA_V2: str = "stateful-episodic-scenario.v2"
EPISODIC_SCENARIO_COUNT: int = 12

# LocalAgent current frozen episodic retrieval limits（显式 mirror，不可复制无出处 magic value）。
EPISODIC_MAX_SELECTED: int = 3
EPISODIC_MAX_CONTEXT_CHARS: int = 1200
# LocalAgent AgentRouter.DIRECT_MEMORY_SCOPE / ORCHESTRATION_MEMORY_SCOPE。
EPISODIC_DIRECT_SCOPE: str = "direct"
SUPPORTED_MEMORY_SCOPES: frozenset[str] = frozenset({"direct", "orchestration"})

# 冻结的 E01..E12 Case inventory（不可重排、不可改变语义）。
FROZEN_EPISODIC_SCENARIOS: tuple[tuple[str, str], ...] = (
    ("E01", "meaningful_success_forms_episode"),
    ("E02", "failed_run_forms_truthful_episode"),
    ("E03", "trivial_run_rejected"),
    ("E04", "origin_run_id_idempotency"),
    ("E05", "factual_grounding"),
    ("E06", "privacy_boundary"),
    ("E07", "similar_cross_run_retrieval"),
    ("E08", "unrelated_rejection"),
    ("E09", "wrong_scope_isolation"),
    ("E10", "failed_episode_retrieval"),
    ("E11", "context_injection"),
    ("E12", "historical_instruction_safety"),
)
_FROZEN_BY_CASE: dict[str, str] = {case: name for case, name in FROZEN_EPISODIC_SCENARIOS}
CROSS_RUN_CASES: frozenset[str] = frozenset({"E07", "E08", "E10", "E11", "E12"})

# 只允许 E09 使用 dataset-controlled initial fixture；只允许 E04 使用 observer-replay seam。
FIXTURE_ONLY_CASE: str = "E09"
REPLAY_ONLY_CASE: str = "E04"

# fake privacy sentinel 前缀（拒绝真实 credential/path）。
FAKE_FIXTURE_PREFIX: str = "FAKE_"

# 真实 secret 形态防护：拒绝把这些形态写进 Dataset privacy 合同。
_REAL_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

# runtime memory UUID 形态：Dataset 不得 hardcode 任何 memory UUID。
_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _frozen_scenario_id(case_code: str) -> str:
    """返回冻结 inventory 对应的 canonical scenario_id。"""
    return f"{case_code.lower()}_{_FROZEN_BY_CASE[case_code]}"


class EpisodicTruthfulnessOrigin(StrEnum):
    """Scenario Ground Truth 的真实性来源（WP6-E 专用，含 DESIGNED_BAD_CASE）。

    - DETERMINISTIC_GROUND_TRUTH：受控/可重复的确定性事实，可进 hard gate denominator。
    - HUMAN_REVIEWED：人工冻结的事实或判定，可进 deterministic denominator。
    - DESIGNED_BAD_CASE：为验证失败/越权/泄密等设计路径而构造的受控样例；不是从真实模型
      实验发现的 REAL_BAD_CASE，禁止写成 ``REAL_BAD_CASE``。
    """

    DETERMINISTIC_GROUND_TRUTH = "DETERMINISTIC_GROUND_TRUTH"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"
    DESIGNED_BAD_CASE = "DESIGNED_BAD_CASE"


class EpisodicEpisodeOriginKind(StrEnum):
    """Episode 的 origin provenance：每个 Episode 必须二选一，禁止混用。

    - RUN_FORMED：由真实 Run formation（finalization observer）形成。
    - DATASET_CONTROLLED_INITIAL_FIXTURE：dataset-controlled initial fixture，仅 E09
      允许，用于 Scope Isolation，只描述 fixture representation，不实现注入。
    """

    RUN_FORMED = "RUN_FORMED"
    DATASET_CONTROLLED_INITIAL_FIXTURE = "DATASET_CONTROLLED_INITIAL_FIXTURE"


class EpisodicRunRole(StrEnum):
    """Run 在 Scenario 中的稳定语义 role（禁止靠数组下标推断）。"""

    FORMATION_SOURCE = "FORMATION_SOURCE"
    RETRIEVAL_QUERY = "RETRIEVAL_QUERY"


class EpisodicMemoryType(StrEnum):
    """Episode 的 memory_type 固定为 EPISODIC（不复用 semantic MemoryType 假装）。"""

    EPISODIC = "EPISODIC"


class EpisodicMemoryStatus(StrEnum):
    """Episode row status；本地 agent 对 Episode 只允许 ACTIVE。"""

    ACTIVE = "ACTIVE"


class EpisodicLogicalKeyPolicy(StrEnum):
    """Episode 的 logical_key 策略；EPISODIC 恒为 NULL。"""

    NULL_REQUIRED = "NULL_REQUIRED"


class EpisodicFormationOutcome(StrEnum):
    """formation outcome（与 LocalAgent EpisodicFormationOutcome 一致的 frozen 值域）。"""

    CREATED = "CREATED"
    REUSED = "REUSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class EpisodicSkipReason(StrEnum):
    """Eligibility skip reason；trivial/no-observation input 统一为 SKIPPED_INELIGIBLE。"""

    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"


class EpisodicTerminalStatus(StrEnum):
    """Run 的 terminal 观察（Episode result 保存真实 terminal）。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EpisodicDeliveryStatus(StrEnum):
    """OutputGate delivery 观察；failed Run 只保存真实 NOT_DELIVERED，不伪造 final answer。"""

    DELIVERED = "DELIVERED"
    NOT_DELIVERED = "NOT_DELIVERED"


class EpisodicObservedStepStatus(StrEnum):
    """Episode observations 中允许持久化的 step status。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NonexistentFactKind(StrEnum):
    """被禁止的 fabricated fact 类别（Dataset 必须能明确表达不存在的 step/tool/recovery/result）。"""

    STEP = "STEP"
    TOOL = "TOOL"
    RECOVERY = "RECOVERY"
    RESULT = "RESULT"


class EpisodicEvaluationControl(StrEnum):
    """narrow typed evaluation capability vocabulary（与 LocalAgent v3 1:1 对齐）。

    禁止 generic ``execute_arbitrary_test_action`` / ``fault_point=arbitrary`` /
    ``fault_behavior=arbitrary``。成员与真实 target wire value 完全一致：
    DETERMINISTIC_FAILED_RUN（E02/E10 Run A）、REPLAY_EPISODIC_FORMATION_OBSERVER
    （仅 E04）、INSTALL_EPISODIC_FIXTURE（仅 E09）、CAPTURE_EPISODIC_PIPELINE
    （Layer1 identity/retrieval/injection capture）、
    DETERMINISTIC_EPISODIC_SUCCESS_RUN（E08 Run A，target-owned allowlisted
    deterministic successful execution profile，禁止发送 plan/steps/tool/prompt）。
    """

    DETERMINISTIC_FAILED_RUN = "DETERMINISTIC_FAILED_RUN"
    REPLAY_EPISODIC_FORMATION_OBSERVER = "REPLAY_EPISODIC_FORMATION_OBSERVER"
    INSTALL_EPISODIC_FIXTURE = "INSTALL_EPISODIC_FIXTURE"
    CAPTURE_EPISODIC_PIPELINE = "CAPTURE_EPISODIC_PIPELINE"
    DETERMINISTIC_EPISODIC_SUCCESS_RUN = "DETERMINISTIC_EPISODIC_SUCCESS_RUN"


#: Target-owned deterministic successful execution profile（LocalAgent
#: ``DETERMINISTIC_EPISODIC_SUCCESS_PROFILE``）。Dataset 只绑定 capability，不发送
#: plan/step/tool/prompt/status override。
DETERMINISTIC_EPISODIC_SUCCESS_PROFILE: str = "DETERMINISTIC_EPISODIC_SUCCESS_V1"


#: 与 LocalAgent ``_LEGAL_CAPABILITY_COMPOSITIONS`` 一致的显式合法组合；任何其它
#: 组合 fail closed（不允许 arbitrary flags uncontrolled composition）。
_LEGAL_CAPABILITY_COMPOSITIONS: frozenset[frozenset[EpisodicEvaluationControl]] = frozenset(
    {
        frozenset(),
        frozenset({EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN}),
        frozenset({EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE}),
        frozenset({EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN}),
        frozenset(
            {
                EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN,
                EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
        frozenset(
            {
                EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN,
                EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
        frozenset({EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER}),
        frozenset({EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE}),
        frozenset(
            {
                EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE,
                EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
        frozenset(
            {
                EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE,
                EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN,
                EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
    }
)


class EpisodicFailureSource(StrEnum):
    """Failed-Run 的 failure provenance：只允许设计型 evaluation fault。

    不把 ``DESIGNED_BAD_CASE`` 升级成 ``REAL_BAD_CASE``；E02/E10 仍是设计型
    Bad Case，failure 由 isolated evaluation fault injection 制造。
    """

    DESIGNED_EVALUATION_FAULT = "DESIGNED_EVALUATION_FAULT"


class EpisodicSourceType(StrEnum):
    """Episode 进入 ContextBuilder 的 typed source type（固定 EPISODIC_MEMORY_RETRIEVAL）。"""

    EPISODIC_MEMORY_RETRIEVAL = "EPISODIC_MEMORY_RETRIEVAL"


class EpisodicContextRole(StrEnum):
    """Episode 的 role/trust 绑定（历史数据固定 USER_CONTENT，禁止升级为指令）。"""

    USER_CONTENT = "USER_CONTENT"


class UsefulnessPolicy(StrEnum):
    """Usefulness 政策：不把 LLM judge expected score 作为 hard Ground Truth。"""

    OBSERVATIONAL_ONLY = "OBSERVATIONAL_ONLY"


class InvariantKind(StrEnum):
    """稳定的 Episode/Retrieval invariant（strict enum，不写 natural-language-only）。"""

    LOGICAL_KEY_NULL = "LOGICAL_KEY_NULL"
    ONE_ROW_PER_ORIGIN_RUN = "ONE_ROW_PER_ORIGIN_RUN"
    DIFFERENT_ORIGIN_MAY_COEXIST = "DIFFERENT_ORIGIN_MAY_COEXIST"
    SEMANTIC_LIFECYCLE_DOES_NOT_MUTATE = "SEMANTIC_LIFECYCLE_DOES_NOT_MUTATE"
    RETRIEVAL_READ_ONLY = "RETRIEVAL_READ_ONLY"
    FORMATION_DOES_NOT_CHANGE_TERMINAL = "FORMATION_DOES_NOT_CHANGE_TERMINAL"
    WRONG_SCOPE_NOT_SELECTED = "WRONG_SCOPE_NOT_SELECTED"


def _matches_real_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _REAL_SECRET_PATTERNS)


def _reject_runtime_uuids(payload: object, *, where: str) -> None:
    """递归拒绝任何匹配 runtime memory UUID 形态的字符串。"""
    if isinstance(payload, str):
        if _UUID_PATTERN.search(payload):
            raise ValueError(f"hardcoded runtime memory UUID-like string is forbidden in {where}: {payload!r}")
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            _reject_runtime_uuids(key, where=where)
            _reject_runtime_uuids(value, where=where)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            _reject_runtime_uuids(item, where=where)


class FormationAssertion(BaseModel):
    """FORMATION：期望的 formation 结果（只对适用 run required）。"""

    model_config = ConfigDict(extra="forbid")

    expected_formation_outcome: EpisodicFormationOutcome
    expected_episode_count_delta: int = Field(ge=0)
    expected_memory_type: EpisodicMemoryType = EpisodicMemoryType.EPISODIC
    expected_status: EpisodicMemoryStatus = EpisodicMemoryStatus.ACTIVE
    expected_origin_run_id: StrictStr
    logical_key_policy: EpisodicLogicalKeyPolicy = EpisodicLogicalKeyPolicy.NULL_REQUIRED
    expected_episode_ref: StrictStr | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "FormationAssertion":
        if self.expected_formation_outcome is EpisodicFormationOutcome.CREATED:
            if self.expected_episode_count_delta != 1:
                raise ValueError(
                    "CREATED formation outcome requires expected_episode_count_delta exactly 1 "
                    "(one authoritative run -> at most one Episode; origin_run_id idempotency)"
                )
            if self.expected_episode_ref is None:
                raise ValueError("CREATED formation outcome requires expected_episode_ref")
        else:
            if self.expected_episode_count_delta != 0:
                raise ValueError(
                    f"{self.expected_formation_outcome.value} formation outcome requires episode_count_delta 0"
                )
            if self.expected_episode_ref is not None:
                raise ValueError(
                    f"{self.expected_formation_outcome.value} formation outcome must not declare expected_episode_ref"
                )
        return self


class EligibilityAssertion(BaseModel):
    """ELIGIBILITY：期望的 formation eligibility 与 skip reason。"""

    model_config = ConfigDict(extra="forbid")

    eligible: bool
    expected_skip_reason: EpisodicSkipReason | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "EligibilityAssertion":
        if self.eligible and self.expected_skip_reason is not None:
            raise ValueError("eligible=true must not declare expected_skip_reason")
        if not self.eligible and self.expected_skip_reason is None:
            raise ValueError("eligible=false requires expected_skip_reason")
        return self


class EpisodeStructureAssertion(BaseModel):
    """EPISODE_STRUCTURE：formed Episode 的 typed 结构期望（不做 canonical_text exact-match）。"""

    model_config = ConfigDict(extra="forbid")

    expected_memory_type: EpisodicMemoryType = EpisodicMemoryType.EPISODIC
    expected_status: EpisodicMemoryStatus = EpisodicMemoryStatus.ACTIVE
    logical_key_policy: EpisodicLogicalKeyPolicy = EpisodicLogicalKeyPolicy.NULL_REQUIRED
    expected_origin_run_id: StrictStr
    expected_agent_id: StrictStr
    expected_memory_scope: StrictStr
    payload_schema_valid: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodeStructureAssertion":
        if self.expected_memory_scope not in SUPPORTED_MEMORY_SCOPES:
            raise ValueError(f"unsupported episode memory_scope: {self.expected_memory_scope!r}")
        return self


class RequiredObservedStep(BaseModel):
    """Grounding 中一个必须被观察到的 step status。"""

    model_config = ConfigDict(extra="forbid")

    step_ref: StrictStr
    expected_status: EpisodicObservedStepStatus

    @field_validator("step_ref")
    @classmethod
    def _step_ref(cls, value: str) -> str:
        return _require_wire_id(value, "step_ref")


class ForbiddenNonexistentFact(BaseModel):
    """Grounding 中禁止出现的 fabricated fact（必须是安全 test fixture）。"""

    model_config = ConfigDict(extra="forbid")

    kind: NonexistentFactKind
    value: StrictStr = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("forbidden fact value must not be blank")
        return value


class GroundingAssertion(BaseModel):
    """EVIDENCE_GROUNDING：Episode 必须 grounded 于真实观察；禁止 invented fact。"""

    model_config = ConfigDict(extra="forbid")

    required_observed_step_statuses: list[RequiredObservedStep] = Field(default_factory=list)
    require_runtime_step_facts: StrictBool = False
    forbidden_nonexistent_facts: list[ForbiddenNonexistentFact] = Field(default_factory=list)
    expected_terminal_status: EpisodicTerminalStatus
    expected_delivery_status: EpisodicDeliveryStatus

    @model_validator(mode="after")
    def _coherent(self) -> "GroundingAssertion":
        step_refs = [item.step_ref for item in self.required_observed_step_statuses]
        if len(step_refs) != len(set(step_refs)):
            raise ValueError("required_observed_step_statuses step_ref must be unique")
        facts: set[tuple[NonexistentFactKind, str]] = set()
        for fact in self.forbidden_nonexistent_facts:
            identity = (fact.kind, fact.value)
            if identity in facts:
                raise ValueError("forbidden_nonexistent_facts must be unique")
            facts.add(identity)
        return self


class PrivacyAssertion(BaseModel):
    """PRIVACY：只允许 fake fixtures；禁止真实 secret/path/credential。"""

    model_config = ConfigDict(extra="forbid")

    must_not_contain_literal: list[StrictStr] = Field(default_factory=list)
    must_not_contain_secret_fixture: list[StrictStr] = Field(default_factory=list)
    must_not_contain_path_fixture: list[StrictStr] = Field(default_factory=list)
    must_not_contain_forbidden_field: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> "PrivacyAssertion":
        all_values = [
            *self.must_not_contain_literal,
            *self.must_not_contain_secret_fixture,
            *self.must_not_contain_path_fixture,
            *self.must_not_contain_forbidden_field,
        ]
        if not all_values:
            raise ValueError("privacy assertion must declare at least one constraint")
        for values in (
            self.must_not_contain_literal,
            self.must_not_contain_secret_fixture,
            self.must_not_contain_path_fixture,
            self.must_not_contain_forbidden_field,
        ):
            if len(values) != len(set(values)):
                raise ValueError("privacy assertion values must be unique within each list")
        for value in [*self.must_not_contain_secret_fixture, *self.must_not_contain_path_fixture]:
            if not value.startswith(FAKE_FIXTURE_PREFIX):
                raise ValueError(f"secret/path fixture must use {FAKE_FIXTURE_PREFIX} sentinel prefix: {value!r}")
        for value in all_values:
            if _matches_real_secret(value):
                raise ValueError(f"privacy value resembles a real credential: {value!r}")
        return self


class EpisodeScoreExpectation(BaseModel):
    """narrow typed per-episode lexical score Ground Truth（无 generic score expression）。

    只绑定 symbolic ``episode_ref`` + 期望的 deterministic ``lexical_match_score``；
    用于冻结 E08 zero-score 与未来 positive score 的 design-time 期望。ultimate
    Layer1 authority 仍以 LocalAgent capture artifact 的真实 score 为准。
    """

    model_config = ConfigDict(extra="forbid")

    episode_ref: StrictStr
    expected_score: int = Field(ge=0)

    @field_validator("episode_ref")
    @classmethod
    def _episode_ref(cls, value: str) -> str:
        return _require_wire_id(value, "episode_score_expectation episode_ref")


class RetrievalAssertion(BaseModel):
    """RETRIEVAL：期望的 candidate/selected/excluded 计数与 symbolic identity。"""

    model_config = ConfigDict(extra="forbid")

    expected_candidate_count: int = Field(ge=0)
    expected_selected_count: int = Field(ge=0)
    expected_selected_episode_identity: list[StrictStr] = Field(default_factory=list)
    expected_excluded_episode_identity: list[StrictStr] = Field(default_factory=list)
    episode_score_expectations: list[EpisodeScoreExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> "RetrievalAssertion":
        selected = self.expected_selected_episode_identity
        excluded = self.expected_excluded_episode_identity
        if len(selected) != len(set(selected)):
            raise ValueError("expected_selected_episode_identity must be unique")
        if len(excluded) != len(set(excluded)):
            raise ValueError("expected_excluded_episode_identity must be unique")
        overlap = set(selected) & set(excluded)
        if overlap:
            raise ValueError(f"episode ref {sorted(overlap)} must not be both selected and excluded")
        for ref in [*selected, *excluded]:
            _require_wire_id(ref, "episode identity ref")
        if len(selected) != self.expected_selected_count:
            raise ValueError("expected_selected_count must equal len(expected_selected_episode_identity)")
        # score expectation consistency：score 0 必须 excluded 且不 selected
        # （LocalAgent lexical scoring 对 score<=0 一律 rejected）；不允许 generic
        # score expression。score>0 由 ranking/budget 决定，不在此强制 selected。
        if self.episode_score_expectations:
            refs = [item.episode_ref for item in self.episode_score_expectations]
            if len(refs) != len(set(refs)):
                raise ValueError("episode_score_expectations must be unique per episode_ref")
            for expectation in self.episode_score_expectations:
                if expectation.expected_score == 0:
                    if expectation.episode_ref not in excluded:
                        raise ValueError(
                            f"episode {expectation.episode_ref} with expected_score 0 must be in "
                            "expected_excluded_episode_identity"
                        )
                    if expectation.episode_ref in selected:
                        raise ValueError(
                            f"episode {expectation.episode_ref} with expected_score 0 must not be selected"
                        )
        return self


class RankingAssertion(BaseModel):
    """RANKING：期望的 symbolic rank order 与 top-K / char budget / zero-score 约束。"""

    model_config = ConfigDict(extra="forbid")

    expected_rank_order: list[StrictStr] = Field(default_factory=list)
    max_selected: int = Field(default=EPISODIC_MAX_SELECTED, ge=1)
    max_chars: int = Field(default=EPISODIC_MAX_CONTEXT_CHARS, ge=1)
    zero_score_exclusion: bool = True

    @field_validator("max_selected")
    @classmethod
    def _max_selected(cls, value: int) -> int:
        if value > EPISODIC_MAX_SELECTED:
            raise ValueError(f"max_selected must not exceed {EPISODIC_MAX_SELECTED}")
        return value

    @field_validator("max_chars")
    @classmethod
    def _max_chars(cls, value: int) -> int:
        if value > EPISODIC_MAX_CONTEXT_CHARS:
            raise ValueError(f"max_chars must not exceed {EPISODIC_MAX_CONTEXT_CHARS}")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "RankingAssertion":
        if self.expected_rank_order:
            if len(self.expected_rank_order) != len(set(self.expected_rank_order)):
                raise ValueError("expected_rank_order must be unique")
            for ref in self.expected_rank_order:
                _require_wire_id(ref, "expected_rank_order ref")
        return self


class ScopeIsolationAssertion(BaseModel):
    """SCOPE_ISOLATION：foreign exact agent/scope Episode 不得 candidate/selected/injected。

    只检查 typed 参与面，不能只检查最终回答文本。
    """

    model_config = ConfigDict(extra="forbid")

    expected_foreign_episode_ref: StrictStr
    expected_candidate: bool = False
    expected_selected: bool = False
    expected_injected: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> "ScopeIsolationAssertion":
        _require_wire_id(self.expected_foreign_episode_ref, "expected_foreign_episode_ref")
        if self.expected_candidate or self.expected_selected or self.expected_injected:
            raise ValueError("scope isolation assertion requires foreign episode fully excluded")
        return self


class InjectionAssertion(BaseModel):
    """INJECTION：selected / supplied / actual builder-injected 三个独立 evidence surface。

    禁止 ``selected=true`` 自动推导 ``injected=true``。
    """

    model_config = ConfigDict(extra="forbid")

    expected_selected: int = Field(ge=0)
    expected_supplied: int = Field(ge=0)
    expected_context_record_count: int = Field(ge=0)
    expected_planning_injected: bool


class TrustBoundaryAssertion(BaseModel):
    """TRUST_BOUNDARY：Episode 以 USER_CONTENT 历史数据进入 planner，不升权。"""

    model_config = ConfigDict(extra="forbid")

    expected_source_type: EpisodicSourceType = EpisodicSourceType.EPISODIC_MEMORY_RETRIEVAL
    expected_role: EpisodicContextRole = EpisodicContextRole.USER_CONTENT
    historical_preamble_required: bool = True
    specialist_visible: bool = False
    synthesis_visible: bool = False


class PersistenceAssertion(BaseModel):
    """PERSISTENCE：final read-only state expectation。

    final SQLite 是 persistence authority，不是 retrieval-selection oracle；DB contains A
    不等于 Runtime selected A。
    """

    model_config = ConfigDict(extra="forbid")

    expected_episode_row_count: int = Field(ge=0)
    origin_run_id_uniqueness: bool = True
    expected_memory_type: EpisodicMemoryType = EpisodicMemoryType.EPISODIC
    expected_status: EpisodicMemoryStatus = EpisodicMemoryStatus.ACTIVE
    logical_key_is_null: bool = True
    expected_agent_ids: list[StrictStr] = Field(min_length=1)
    expected_memory_scopes: list[StrictStr] = Field(min_length=1)
    typed_payload_valid: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> "PersistenceAssertion":
        if len(self.expected_agent_ids) != len(set(self.expected_agent_ids)):
            raise ValueError("expected_agent_ids must be unique")
        for scope in self.expected_memory_scopes:
            if scope not in SUPPORTED_MEMORY_SCOPES:
                raise ValueError(f"unsupported persistence memory_scope: {scope!r}")
        return self


class IdempotencyAssertion(BaseModel):
    """IDEMPOTENCY：same authoritative formation observer 被 replay；一 row / REUSED。"""

    model_config = ConfigDict(extra="forbid")

    replay_target_run_id: StrictStr
    expected_total_row_count_delta: int = Field(ge=0)
    expected_first_outcome: EpisodicFormationOutcome = EpisodicFormationOutcome.CREATED
    expected_second_outcome: EpisodicFormationOutcome = EpisodicFormationOutcome.REUSED

    @model_validator(mode="after")
    def _coherent(self) -> "IdempotencyAssertion":
        if self.expected_first_outcome is not EpisodicFormationOutcome.CREATED:
            raise ValueError("idempotency first formation outcome must be CREATED")
        if self.expected_second_outcome is not EpisodicFormationOutcome.REUSED:
            raise ValueError("idempotency second formation outcome must be REUSED (NO_CHANGE)")
        return self


class InvariantAssertion(BaseModel):
    """INVARIANT：scenario-wide stable checks（strict enum）。"""

    model_config = ConfigDict(extra="forbid")

    required_invariants: list[InvariantKind] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> "InvariantAssertion":
        if len(self.required_invariants) != len(set(self.required_invariants)):
            raise ValueError("required_invariants must be unique")
        return self


class EpisodicScenarioAssertionGroups(BaseModel):
    """Scenario-wide typed assertion groups（run-scoped groups 位于 EpisodicRun）。"""

    model_config = ConfigDict(extra="forbid")

    persistence: PersistenceAssertion
    idempotency: IdempotencyAssertion | None = None
    invariant: InvariantAssertion


class EpisodicFixtureObservation(BaseModel):
    """Fixture 中一条 typed observation（与 LocalAgent EpisodicFixtureObservation 对齐）。"""

    model_config = ConfigDict(extra="forbid")

    observation_type: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    status: StrictStr = Field(min_length=1)
    safe_error_code: StrictStr | None = None
    outcome_classification: StrictStr | None = None
    result_digest: StrictStr | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicFixtureObservation":
        for value, name in (
            (self.safe_error_code, "safe_error_code"),
            (self.outcome_classification, "outcome_classification"),
            (self.result_digest, "result_digest"),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"fixture observation {name} must not be blank")
        return self


class EpisodicFixtureResult(BaseModel):
    """Fixture 的 typed result（与 LocalAgent EpisodicFixtureResult 对齐）。"""

    model_config = ConfigDict(extra="forbid")

    terminal_status: StrictStr = Field(min_length=1)
    stop_reason: StrictStr = Field(min_length=1)
    delivery_status: StrictStr = Field(min_length=1)


class EpisodicInitialFixture(BaseModel):
    """E09 dataset-controlled initial fixture（与 LocalAgent ``EpisodicFixtureSpec`` 对齐）。

    只描述 foreign exact agent/scope Episode fixture 的完整 typed shape：situation /
    goal / observations / result / lesson。``canonical_text`` 必须由 target
    ``render_episode_canonical_text()`` 生成，Dataset 禁止携带 caller canonical_text。
    未来 fixture 必须由 approved isolated evaluation harness 注入，artifact 必须保留
    ``episode_origin_kind`` provenance。
    """

    model_config = ConfigDict(extra="forbid")

    fixture_ref: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    origin_run_id: StrictStr
    situation: StrictStr = Field(min_length=1)
    goal: StrictStr = Field(min_length=1)
    observations: list[EpisodicFixtureObservation] = Field(min_length=1)
    result: EpisodicFixtureResult
    lesson: StrictStr | None = None

    @field_validator("fixture_ref")
    @classmethod
    def _fixture_ref(cls, value: str) -> str:
        return _require_wire_id(value, "fixture_ref")

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicInitialFixture":
        for value, name in (
            (self.agent_id, "agent_id"),
            (self.memory_scope, "memory_scope"),
            (self.origin_run_id, "origin_run_id"),
        ):
            if not value.strip():
                raise ValueError(f"fixture {name} must not be blank")
        if self.memory_scope not in SUPPORTED_MEMORY_SCOPES:
            raise ValueError(f"unsupported fixture memory_scope: {self.memory_scope!r}")
        if self.memory_scope == EPISODIC_DIRECT_SCOPE:
            raise ValueError("foreign scope-isolation fixture must not use the run direct scope")
        for value, name in (
            (self.situation, "situation"),
            (self.goal, "goal"),
        ):
            if len(value) > 400:
                raise ValueError(f"fixture {name} exceeds the bounded length limit")
        if len(self.observations) > 8:
            raise ValueError("fixture observations must not exceed the bounded count")
        if self.lesson is not None and not self.lesson.strip():
            raise ValueError("fixture lesson must not be blank")
        return self


class EpisodicEvaluationControlDeclaration(BaseModel):
    """Run-level strict typed evaluation control（与 LocalAgent v3 对齐）。

    - ``capabilities``：narrow typed capability vocabulary，组合必须属于
      ``_LEGAL_CAPABILITY_COMPOSITIONS``。
    - ``fixture_ref``：Dataset 侧 symbolic 绑定到 scenario ``initial_fixture``
      （完整 typed ``EpisodicFixtureSpec`` 由 runner 从 initial_fixture 构造并发送到
      LocalAgent v3）；仅 INSTALL_EPISODIC_FIXTURE 允许。
    - ``replay_run_id``：Dataset 侧 symbolic run_id 引用（runner 映射到真实 runtime
      run UUID）；仅 REPLAY_EPISODIC_FORMATION_OBSERVER 允许。
    - 不承载 ``fault_point`` / ``fault_behavior`` / ``execute_python`` 等任意配置。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["episodic-evaluation-control.v1"] = "episodic-evaluation-control.v1"
    capabilities: list[EpisodicEvaluationControl] = Field(default_factory=list)
    fixture_ref: StrictStr | None = None
    replay_run_id: StrictStr | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicEvaluationControlDeclaration":
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("evaluation control capabilities must be unique")
        composition = frozenset(self.capabilities)
        if composition not in _LEGAL_CAPABILITY_COMPOSITIONS:
            raise ValueError("evaluation control capability composition is not explicitly allowlisted")
        has_fixture = EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE in composition
        if has_fixture and not isinstance(self.fixture_ref, str):
            raise ValueError("INSTALL_EPISODIC_FIXTURE requires fixture_ref")
        if not has_fixture and self.fixture_ref is not None:
            raise ValueError("fixture_ref is only allowed with INSTALL_EPISODIC_FIXTURE")
        has_replay = EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER in composition
        if has_replay and not isinstance(self.replay_run_id, str):
            raise ValueError("REPLAY_EPISODIC_FORMATION_OBSERVER requires replay_run_id")
        if not has_replay and self.replay_run_id is not None:
            raise ValueError("replay_run_id is only allowed with REPLAY_EPISODIC_FORMATION_OBSERVER")
        if has_fixture and self.fixture_ref is not None:
            _require_wire_id(self.fixture_ref, "evaluation control fixture_ref")
        if has_replay and self.replay_run_id is not None:
            _require_wire_id(self.replay_run_id, "evaluation control replay_run_id")
        return self

    @property
    def capability_set(self) -> frozenset[EpisodicEvaluationControl]:
        """返回 capabilities 的 frozenset 视图（供 composition 校验）。"""
        return frozenset(self.capabilities)


class EpisodicEpisodeBinding(BaseModel):
    """Symbolic Episode identity：把稳定 episode_ref 映射到 origin run / fixture。"""

    model_config = ConfigDict(extra="forbid")

    episode_ref: StrictStr
    origin_kind: EpisodicEpisodeOriginKind
    origin_run_id: StrictStr | None = None

    @field_validator("episode_ref")
    @classmethod
    def _episode_ref(cls, value: str) -> str:
        return _require_wire_id(value, "episode_ref")

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicEpisodeBinding":
        if self.origin_kind is EpisodicEpisodeOriginKind.RUN_FORMED:
            if self.origin_run_id is None:
                raise ValueError("RUN_FORMED episode binding requires origin_run_id")
        else:
            if self.origin_run_id is not None:
                raise ValueError("fixture-kind episode binding must not declare origin_run_id")
        return self


class EpisodicRun(BaseModel):
    """Scenario 中一个 ordered Run；携带 run-scoped typed assertion groups。

    同 scenario 的 Runs 共享同一 isolated Memory DB，但每 Run 使用新的 runtime run
    identity；不同 scenario 使用 fresh isolated environment。本 Dataset 只描述 metadata /
    schema，不实现 provisioner。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: StrictStr
    run_role: EpisodicRunRole
    agent_id: StrictStr
    memory_scope: StrictStr
    user_request: StrictStr = Field(min_length=1)
    expected_formation: FormationAssertion | None = None
    expected_eligibility: EligibilityAssertion | None = None
    expected_episode_structure: EpisodeStructureAssertion | None = None
    expected_grounding: GroundingAssertion | None = None
    expected_privacy: PrivacyAssertion | None = None
    expected_retrieval: RetrievalAssertion | None = None
    expected_ranking: RankingAssertion | None = None
    expected_scope_isolation: ScopeIsolationAssertion | None = None
    expected_injection: InjectionAssertion | None = None
    expected_trust_boundary: TrustBoundaryAssertion | None = None
    evaluation_control: EpisodicEvaluationControlDeclaration | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        return _require_wire_id(value, "run_id")

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.core.evaluation.immutable import freeze_json

        if not value:
            return value
        try:
            freeze_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"run metadata must be JSON-compatible: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicRun":
        if self.memory_scope != EPISODIC_DIRECT_SCOPE:
            raise ValueError("episodic run must use the direct memory scope")
        if self.run_role is EpisodicRunRole.RETRIEVAL_QUERY and self.expected_retrieval is None:
            raise ValueError("RETRIEVAL_QUERY run requires expected_retrieval")
        if self.expected_scope_isolation is not None and self.expected_retrieval is None:
            raise ValueError("scope isolation assertion requires an expected_retrieval assertion")
        return self


class EpisodicScenario(BaseModel):
    """一个 evaluation-only stateful episodic scenario（Dataset Ground Truth 单元）。

    ``Scenario -> ordered Runs -> typed assertion groups -> final read-only state
    expectation``。每个 ordered Run 保存一个 persisted ExecutionAttempt，
    ``case_id = <scenario_id>.<run_id>``。
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr
    case_code: StrictStr
    description: StrictStr = Field(min_length=1)
    truthfulness_origin: EpisodicTruthfulnessOrigin
    episode_origin_kind: EpisodicEpisodeOriginKind
    runs: list[EpisodicRun] = Field(min_length=1)
    episodes: list[EpisodicEpisodeBinding] = Field(default_factory=list)
    initial_fixture: EpisodicInitialFixture | None = None
    assertion_groups: EpisodicScenarioAssertionGroups
    failure_source: EpisodicFailureSource | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id(cls, value: str) -> str:
        return _require_wire_id(value, "scenario_id")

    @field_validator("case_code")
    @classmethod
    def _case_code(cls, value: str) -> str:
        return _require_wire_id(value, "case_code")

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.core.evaluation.immutable import freeze_json

        if not value:
            return value
        try:
            freeze_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metadata must be JSON-compatible: {exc}") from exc
        return value

    def _binding(self, episode_ref: str) -> EpisodicEpisodeBinding | None:
        return next((item for item in self.episodes if item.episode_ref == episode_ref), None)

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicScenario":
        # ---- frozen inventory identity ----
        frozen_name = _FROZEN_BY_CASE.get(self.case_code)
        if frozen_name is None:
            raise ValueError(f"unknown case_code: {self.case_code!r}; expected E01..E12")
        expected_scenario_id = _frozen_scenario_id(self.case_code)
        if self.scenario_id != expected_scenario_id:
            raise ValueError(f"scenario_id {self.scenario_id!r} must equal {expected_scenario_id!r}")

        # ---- runs ----
        run_ids = [run.run_id for run in self.runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("duplicate run_id within a scenario is not allowed")
        expected_run_count = 2 if self.case_code in CROSS_RUN_CASES else 1
        if len(self.runs) != expected_run_count:
            raise ValueError(f"case_code {self.case_code} requires {expected_run_count} run(s), got {len(self.runs)}")
        roles = [run.run_role for run in self.runs]
        if self.case_code in CROSS_RUN_CASES:
            if roles != [EpisodicRunRole.FORMATION_SOURCE, EpisodicRunRole.RETRIEVAL_QUERY]:
                raise ValueError("cross-run scenario must declare run_a=FORMATION_SOURCE then run_b=RETRIEVAL_QUERY")
        elif self.case_code == FIXTURE_ONLY_CASE:
            if roles != [EpisodicRunRole.RETRIEVAL_QUERY]:
                raise ValueError("E09 must declare a single RETRIEVAL_QUERY run")
        else:
            if roles != [EpisodicRunRole.FORMATION_SOURCE]:
                raise ValueError("single-run scenario must declare a single FORMATION_SOURCE run")

        # ---- episode origin kind & fixture boundary ----
        if self.episode_origin_kind is EpisodicEpisodeOriginKind.RUN_FORMED:
            if self.initial_fixture is not None:
                raise ValueError("RUN_FORMED scenario must not declare initial_fixture")
            if any(
                item.origin_kind is EpisodicEpisodeOriginKind.DATASET_CONTROLLED_INITIAL_FIXTURE
                for item in self.episodes
            ):
                raise ValueError("RUN_FORMED scenario must not declare fixture-kind episode bindings")
        else:
            if self.case_code != FIXTURE_ONLY_CASE:
                raise ValueError(
                    f"only E09 may use DATASET_CONTROLLED_INITIAL_FIXTURE episode origin (got {self.case_code})"
                )
            if self.initial_fixture is None:
                raise ValueError("E09 requires an initial_fixture")
            if not any(
                run.agent_id != self.initial_fixture.agent_id or run.memory_scope != self.initial_fixture.memory_scope
                for run in self.runs
            ):
                raise ValueError("E09 fixture must be foreign to every run agent/scope")

        # ---- episode bindings ----
        episode_refs = [item.episode_ref for item in self.episodes]
        if len(episode_refs) != len(set(episode_refs)):
            raise ValueError("duplicate episode_ref is not allowed")
        for item in self.episodes:
            if item.origin_kind is EpisodicEpisodeOriginKind.RUN_FORMED:
                if item.origin_run_id not in run_ids:
                    raise ValueError(
                        f"episode {item.episode_ref} origin_run_id {item.origin_run_id!r} not declared in runs"
                    )
            else:
                if self.initial_fixture is None or item.episode_ref != self.initial_fixture.fixture_ref:
                    raise ValueError(
                        f"fixture-kind episode {item.episode_ref} must reference the scenario initial_fixture"
                    )

        # ---- run-scoped assertion coherence ----
        for run in self.runs:
            if run.expected_formation is not None:
                if run.expected_formation.expected_origin_run_id != run.run_id:
                    raise ValueError(f"run {run.run_id} formation expected_origin_run_id must equal the run_id")
                if run.expected_formation.expected_episode_ref is not None:
                    binding = self._binding(run.expected_formation.expected_episode_ref)
                    if binding is None or binding.origin_run_id != run.run_id:
                        raise ValueError(
                            f"run {run.run_id} formation episode_ref must be a RUN_FORMED binding of this run"
                        )
            if run.expected_episode_structure is not None:
                structure = run.expected_episode_structure
                if structure.expected_origin_run_id != run.run_id:
                    raise ValueError(f"run {run.run_id} structure expected_origin_run_id must equal the run_id")
                if structure.expected_agent_id != run.agent_id:
                    raise ValueError(f"run {run.run_id} structure expected_agent_id must equal the run agent_id")
                if structure.expected_memory_scope != run.memory_scope:
                    raise ValueError(f"run {run.run_id} structure expected_memory_scope must equal the run scope")
            if run.expected_privacy is not None and self.case_code != "E06":
                raise ValueError("only E06 may declare a privacy assertion")
            if run.expected_scope_isolation is not None and self.case_code != FIXTURE_ONLY_CASE:
                raise ValueError("only E09 may declare a scope isolation assertion")
            if run.expected_trust_boundary is not None and self.case_code != "E12":
                raise ValueError("only E12 may declare a trust boundary assertion")
            if run.expected_retrieval is not None:
                selected = run.expected_retrieval.expected_selected_episode_identity
                excluded = run.expected_retrieval.expected_excluded_episode_identity
                for ref in [*selected, *excluded]:
                    if self._binding(ref) is None:
                        raise ValueError(f"run {run.run_id} retrieval references unknown episode_ref {ref!r}")
                for expectation in run.expected_retrieval.episode_score_expectations:
                    if self._binding(expectation.episode_ref) is None:
                        raise ValueError(
                            f"run {run.run_id} episode_score_expectation references unknown episode_ref "
                            f"{expectation.episode_ref!r}"
                        )
                if any(item.expected_score == 0 for item in run.expected_retrieval.episode_score_expectations):
                    if run.expected_ranking is None or not run.expected_ranking.zero_score_exclusion:
                        raise ValueError(
                            f"run {run.run_id} zero-score expectation requires expected_ranking zero_score_exclusion=true"
                        )
                if run.expected_ranking is not None:
                    if set(run.expected_ranking.expected_rank_order) != set(selected):
                        raise ValueError(
                            f"run {run.run_id} expected_rank_order must cover exactly the selected episode identities"
                        )
                if run.expected_scope_isolation is not None:
                    foreign = run.expected_scope_isolation.expected_foreign_episode_ref
                    binding = self._binding(foreign)
                    if (
                        binding is None
                        or binding.origin_kind is not EpisodicEpisodeOriginKind.DATASET_CONTROLLED_INITIAL_FIXTURE
                    ):
                        raise ValueError(
                            f"run {run.run_id} expected_foreign_episode_ref must reference a fixture episode binding"
                        )
            if run.expected_ranking is not None and run.expected_retrieval is None:
                raise ValueError(f"run {run.run_id} expected_ranking requires expected_retrieval")

        # ---- E03 eligibility contract ----
        if self.case_code == "E03":
            run = self.runs[0]
            if run.expected_eligibility is None or run.expected_eligibility.eligible:
                raise ValueError("E03 requires eligible=false eligibility assertion")
            if run.expected_eligibility.expected_skip_reason is not EpisodicSkipReason.SKIPPED_INELIGIBLE:
                raise ValueError("E03 skip reason must be SKIPPED_INELIGIBLE")
            if run.expected_formation is None:
                raise ValueError("E03 requires a formation assertion")
            if run.expected_formation.expected_formation_outcome is not EpisodicFormationOutcome.SKIPPED:
                raise ValueError("E03 formation outcome must be SKIPPED")
            if run.expected_formation.expected_episode_count_delta != 0:
                raise ValueError("E03 formation episode_count_delta must be 0")
            if run.expected_episode_structure is not None or run.expected_grounding is not None:
                raise ValueError("E03 must not declare structure/grounding (no episode is formed)")
        else:
            for run in self.runs:
                if run.expected_formation is None:
                    raise ValueError(f"run {run.run_id} requires expected_formation")
                if run.expected_episode_structure is None:
                    raise ValueError(f"run {run.run_id} requires expected_episode_structure")
                if run.expected_grounding is None:
                    raise ValueError(f"run {run.run_id} requires expected_grounding")

        # ---- evaluation control contract（strict typed；与 LocalAgent v3 对齐）----
        # E08：Run A 必须显式声明 DETERMINISTIC_EPISODIC_SUCCESS_RUN（target-owned
        # allowlisted deterministic successful execution profile）；Run B 不得声明该 capability。
        if self.case_code == "E08":
            run_a = self.runs[0]
            run_b = self.runs[1]
            if run_a.evaluation_control is None or (
                EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN
                not in run_a.evaluation_control.capability_set
            ):
                raise ValueError("E08 run_a requires DETERMINISTIC_EPISODIC_SUCCESS_RUN evaluation control")
            if run_b.evaluation_control is not None and (
                EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN in run_b.evaluation_control.capability_set
            ):
                raise ValueError("E08 run_b must not declare DETERMINISTIC_EPISODIC_SUCCESS_RUN evaluation control")

        # 每个带 expected_retrieval 的 run 必须声明 CAPTURE_EPISODIC_PIPELINE
        # （identity/retrieval/injection capture；Formation-only scenario 不强制）。
        for run in self.runs:
            if run.expected_retrieval is not None:
                if run.evaluation_control is None or (
                    EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE not in run.evaluation_control.capability_set
                ):
                    raise ValueError(
                        f"run {run.run_id} (retrieval identity) requires CAPTURE_EPISODIC_PIPELINE evaluation control"
                    )

        # E02：failed-run control 必须绑定到真实 target seam。
        if self.case_code == "E02":
            run = self.runs[0]
            if run.evaluation_control is None or (
                EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN not in run.evaluation_control.capability_set
            ):
                raise ValueError("E02 requires DETERMINISTIC_FAILED_RUN evaluation control")

        # E10：Run A 必须 DETERMINISTIC_FAILED_RUN，Run B 禁止再次启用 failed-run control。
        if self.case_code == "E10":
            run_a = self.runs[0]
            if run_a.evaluation_control is None or (
                EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN not in run_a.evaluation_control.capability_set
            ):
                raise ValueError("E10 run_a requires DETERMINISTIC_FAILED_RUN evaluation control")
            run_b = self.runs[1]
            if run_b.evaluation_control is not None and (
                EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN in run_b.evaluation_control.capability_set
            ):
                raise ValueError("E10 run_b must not declare DETERMINISTIC_FAILED_RUN evaluation control")

        # failure provenance：只有声明 DETERMINISTIC_FAILED_RUN 的 scenario 才能标记
        # DESIGNED_EVALUATION_FAULT；不把设计型 Bad Case 升级成真实事故。
        any_failed_run = any(
            run.evaluation_control is not None
            and EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN in run.evaluation_control.capability_set
            for run in self.runs
        )
        if any_failed_run and self.failure_source is not EpisodicFailureSource.DESIGNED_EVALUATION_FAULT:
            raise ValueError("DETERMINISTIC_FAILED_RUN scenario requires failure_source=DESIGNED_EVALUATION_FAULT")
        if not any_failed_run and self.failure_source is not None:
            raise ValueError("failure_source is only allowed for DETERMINISTIC_FAILED_RUN scenarios")

        # E04 replay seam（仅允许 replay control）。
        if self.case_code == REPLAY_ONLY_CASE:
            run = self.runs[0]
            if run.evaluation_control is None or (
                EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER
                not in run.evaluation_control.capability_set
            ):
                raise ValueError("E04 requires REPLAY_EPISODIC_FORMATION_OBSERVER evaluation control")
            if (
                run.expected_formation is None
                or run.expected_formation.expected_formation_outcome is not EpisodicFormationOutcome.CREATED
            ):
                raise ValueError("E04 run requires CREATED first formation outcome")
            replay_run_id = run.evaluation_control.replay_run_id
            if replay_run_id != run.run_id:
                raise ValueError("E04 replay_run_id must reference the authoritative run")
            idempotency = self.assertion_groups.idempotency
            if idempotency is None:
                raise ValueError("E04 requires an idempotency assertion group")
            if idempotency.replay_target_run_id not in run_ids:
                raise ValueError("idempotency replay_target_run_id must be a declared run")
            if replay_run_id != idempotency.replay_target_run_id:
                raise ValueError("E04 evaluation control replay_run_id must match idempotency replay_target_run_id")
        elif self.assertion_groups.idempotency is not None:
            raise ValueError("only E04 may declare an idempotency assertion group")

        # E09：只允许 fixture install control（+ optional capture），fixture_ref 必须
        # 绑定到 scenario initial_fixture。
        if self.case_code == FIXTURE_ONLY_CASE:
            run = self.runs[0]
            if run.evaluation_control is None or (
                EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE not in run.evaluation_control.capability_set
            ):
                raise ValueError("E09 requires INSTALL_EPISODIC_FIXTURE evaluation control")
            if run.evaluation_control.capability_set - {
                EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE,
                EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE,
            }:
                raise ValueError("E09 may only declare INSTALL_EPISODIC_FIXTURE + CAPTURE_EPISODIC_PIPELINE")
            if self.initial_fixture is None:
                raise ValueError("E09 evaluation control fixture binding requires an initial_fixture")
            if run.evaluation_control.fixture_ref != self.initial_fixture.fixture_ref:
                raise ValueError(
                    "E09 evaluation control fixture_ref must reference the scenario initial_fixture fixture_ref"
                )

        # 其它 scenario：禁止 DETERMINISTIC_FAILED_RUN / REPLAY / INSTALL_FIXTURE /
        # DETERMINISTIC_EPISODIC_SUCCESS_RUN（非授权）。
        for run in self.runs:
            control = run.evaluation_control
            if control is None:
                continue
            forbidden = (
                {EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN}
                | {EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER}
                | {EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE}
                | {EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN}
            )
            if self.case_code in {"E02", "E10"} and run.run_id == "run_a":
                forbidden = forbidden - {EpisodicEvaluationControl.DETERMINISTIC_FAILED_RUN}
            if self.case_code == FIXTURE_ONLY_CASE:
                forbidden = forbidden - {EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE}
            if self.case_code == REPLAY_ONLY_CASE:
                forbidden = forbidden - {EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER}
            if self.case_code == "E08" and run.run_id == "run_a":
                forbidden = forbidden - {EpisodicEvaluationControl.DETERMINISTIC_EPISODIC_SUCCESS_RUN}
            if control.capability_set & forbidden:
                raise ValueError(
                    f"run {run.run_id} declares an evaluation control capability not allowed for {self.case_code}"
                )

        # E08：zero-score Ground Truth 必须冻结 run_a_episode score=0。
        if self.case_code == "E08":
            run_b = self.runs[1]
            retrieval = run_b.expected_retrieval
            assert retrieval is not None
            score_expectations = {
                item.episode_ref: item.expected_score for item in retrieval.episode_score_expectations
            }
            if score_expectations.get("run_a_episode") != 0:
                raise ValueError("E08 requires expected zero lexical score (0) for run_a_episode")
            if retrieval.expected_selected_count != 0:
                raise ValueError("E08 requires expected_selected_count 0")
            if "run_a_episode" not in retrieval.expected_excluded_episode_identity:
                raise ValueError("E08 requires run_a_episode to be excluded (zero-score rejection)")
            if run_b.expected_ranking is None or not run_b.expected_ranking.zero_score_exclusion:
                raise ValueError("E08 requires zero_score_exclusion=true")

        # ---- E12 trust boundary ----
        if self.case_code == "E12":
            if not any(run.expected_trust_boundary is not None for run in self.runs):
                raise ValueError("E12 requires a trust boundary assertion")

        # ---- persistence vs declared episodes ----
        persistence = self.assertion_groups.persistence
        if persistence.expected_episode_row_count != len(self.episodes):
            raise ValueError(
                f"expected_episode_row_count {persistence.expected_episode_row_count} must equal "
                f"declared episode count {len(self.episodes)}"
            )
        return self


class EpisodicDataset(BaseModel):
    """``stateful-episodic-scenario.v1`` 冻结的 12-scenario 集合（测试资产，非业务数据）。"""

    model_config = ConfigDict(extra="forbid")

    dataset_schema_version: StrictStr
    dataset_id: StrictStr
    version: StrictStr
    name: StrictStr = Field(min_length=1)
    description: StrictStr | None = None
    scenarios: list[EpisodicScenario]
    identity_evidence_by_layer: IdentityEvidenceByLayer
    usefulness_policy: UsefulnessPolicy = UsefulnessPolicy.OBSERVATIONAL_ONLY
    content_digest: StrictStr | None = None

    @field_validator("dataset_schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value not in {EPISODIC_DATASET_SCHEMA_V1, EPISODIC_DATASET_SCHEMA_V2}:
            raise ValueError(
                f"unsupported episodic dataset schema version: {value}; "
                f"expected {EPISODIC_DATASET_SCHEMA_V1} or {EPISODIC_DATASET_SCHEMA_V2}"
            )
        return value

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id(cls, value: str) -> str:
        if value not in {EPISODIC_DATASET_ID, EPISODIC_DATASET_V2_ID}:
            raise ValueError(f"dataset_id must be {EPISODIC_DATASET_ID} or {EPISODIC_DATASET_V2_ID}, got {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value not in {EPISODIC_DATASET_VERSION, EPISODIC_DATASET_V2_VERSION}:
            raise ValueError(f"unsupported episodic dataset version: {value!r}")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "EpisodicDataset":
        expected_identity = (
            EPISODIC_DATASET_SCHEMA_V1,
            EPISODIC_DATASET_ID,
            EPISODIC_DATASET_VERSION,
        )
        actual_identity = (self.dataset_schema_version, self.dataset_id, self.version)
        if actual_identity not in {
            expected_identity,
            (EPISODIC_DATASET_SCHEMA_V2, EPISODIC_DATASET_V2_ID, EPISODIC_DATASET_V2_VERSION),
        }:
            raise ValueError(
                "episodic dataset schema/id/version lineage is incoherent; version must be v1 for v1 identity"
            )
        is_v2 = self.dataset_schema_version == EPISODIC_DATASET_SCHEMA_V2
        for scenario in self.scenarios:
            for run in scenario.runs:
                grounding = run.expected_grounding
                if grounding is None:
                    continue
                if is_v2:
                    if grounding.required_observed_step_statuses:
                        raise ValueError("V2 Episodic grounding must not predefine Planner task identities")
                    if not grounding.require_runtime_step_facts:
                        raise ValueError("V2 Episodic grounding requires runtime step evidence")
                elif not grounding.required_observed_step_statuses:
                    raise ValueError("V1 grounding requires observed step status expectations")
        if len(self.scenarios) != EPISODIC_SCENARIO_COUNT:
            raise ValueError(
                f"dataset must declare exactly {EPISODIC_SCENARIO_COUNT} scenarios, got {len(self.scenarios)}"
            )
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("duplicate scenario_id is not allowed")
        case_codes = [scenario.case_code for scenario in self.scenarios]
        if len(case_codes) != len(set(case_codes)):
            raise ValueError("duplicate case_code is not allowed")
        if set(case_codes) != set(_FROZEN_BY_CASE):
            raise ValueError(f"scenario inventory must be exactly E01..E12, got {sorted(case_codes)}")
        # 禁止任何 runtime memory UUID hardcode。
        _reject_runtime_uuids(self.model_dump(mode="json"), where="episodic dataset")
        # Layer contracts：Layer1 identity REQUIRED，Layer2 identity EXPECTED_LIMITATION。
        if self.identity_evidence_by_layer.layer_1 is not IdentityEvidenceRequirement.REQUIRED:
            raise ValueError("Layer1 identity evidence must be REQUIRED")
        if self.identity_evidence_by_layer.layer_2 is not IdentityEvidenceRequirement.EXPECTED_LIMITATION:
            raise ValueError("Layer2 identity evidence must be EXPECTED_LIMITATION")
        if self.usefulness_policy is not UsefulnessPolicy.OBSERVATIONAL_ONLY:
            raise ValueError("usefulness must be OBSERVATIONAL_ONLY (no LLM judge hard gate)")
        return self

    def __len__(self) -> int:
        return len(self.scenarios)


def validate_episodic_dataset(payload: object) -> EpisodicDataset:
    """校验 ``stateful-episodic-scenario.v1`` dataset payload；失败抛 pydantic ValidationError。"""
    return EpisodicDataset.model_validate(payload)


def _load_raw_episodic_payload(path: str | Path) -> tuple[bytes, dict[str, Any]]:
    """读取并解析 UTF-8 JSON episodic dataset 文件；返回 (raw_bytes, payload)。"""
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise EvaluationDatasetLoadError(f"cannot read episodic dataset file: {file_path}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationDatasetLoadError(f"episodic dataset file is not UTF-8: {file_path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvaluationDatasetLoadError(f"episodic dataset file is not valid JSON: {file_path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationDatasetLoadError(f"episodic dataset file must contain a JSON object: {file_path}")
    return raw, payload


def load_episodic_dataset(path: str | Path) -> EpisodicDataset:
    """从 UTF-8 JSON 文件加载并严格校验 ``stateful-episodic-scenario.v1`` dataset。

    Args:
        path: UTF-8 JSON dataset 文件路径。

    Returns:
        校验通过并携带 ``content_digest`` 的 EpisodicDataset。

    Raises:
        EvaluationDatasetLoadError: 文件不可读、不是合法 UTF-8 JSON object。
        pydantic.ValidationError: 内容不符合 episodic dataset schema。
    """
    raw, payload = _load_raw_episodic_payload(path)
    dataset = EpisodicDataset.model_validate(payload)
    return dataset.model_copy(update={"content_digest": content_digest(raw)})


def episodic_dataset_bytes(dataset: EpisodicDataset) -> bytes:
    """把 dataset 投影为稳定的 canonical UTF-8 JSON bytes（用于 round-trip/digest 比较）。"""
    data = dataset.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"content_digest"},
    )
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def episodic_dataset_digest(path: str | Path) -> str:
    """返回 episodic dataset 原始 UTF-8 bytes 的 sha256 digest（DATASET_CANDIDATE_DIGEST）。"""
    raw, _ = _load_raw_episodic_payload(path)
    return content_digest(raw)


__all__ = [
    "CROSS_RUN_CASES",
    "DETERMINISTIC_EPISODIC_SUCCESS_PROFILE",
    "EPISODIC_DATASET_ID",
    "EPISODIC_DATASET_VERSION",
    "EPISODIC_DIRECT_SCOPE",
    "EPISODIC_MAX_CONTEXT_CHARS",
    "EPISODIC_MAX_SELECTED",
    "EPISODIC_SCENARIO_COUNT",
    "EligibilityAssertion",
    "EpisodeScoreExpectation",
    "EpisodicDataset",
    "EpisodicDeliveryStatus",
    "EpisodicEpisodeBinding",
    "EpisodicEpisodeOriginKind",
    "EpisodicEvaluationControl",
    "EpisodicEvaluationControlDeclaration",
    "EpisodicFailureSource",
    "EpisodicFixtureObservation",
    "EpisodicFixtureResult",
    "EpisodicFormationOutcome",
    "EpisodicInitialFixture",
    "EpisodicLogicalKeyPolicy",
    "EpisodicMemoryStatus",
    "EpisodicMemoryType",
    "EpisodicRun",
    "EpisodicRunRole",
    "EpisodicScenario",
    "EpisodicScenarioAssertionGroups",
    "EpisodicSkipReason",
    "EpisodicSourceType",
    "EpisodicTerminalStatus",
    "EpisodicTruthfulnessOrigin",
    "FAKE_FIXTURE_PREFIX",
    "FIXTURE_ONLY_CASE",
    "FormationAssertion",
    "ForbiddenNonexistentFact",
    "FROZEN_EPISODIC_SCENARIOS",
    "GroundingAssertion",
    "IdempotencyAssertion",
    "InvariantAssertion",
    "InvariantKind",
    "NonexistentFactKind",
    "PersistenceAssertion",
    "PrivacyAssertion",
    "RankingAssertion",
    "RequiredObservedStep",
    "RetrievalAssertion",
    "ScopeIsolationAssertion",
    "SUPPORTED_MEMORY_SCOPES",
    "TrustBoundaryAssertion",
    "UsefulnessPolicy",
    "content_digest",
    "episodic_dataset_bytes",
    "episodic_dataset_digest",
    "load_episodic_dataset",
    "validate_episodic_dataset",
]
