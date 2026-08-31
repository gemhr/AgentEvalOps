"""WP6-E typed step identity normalization owner（单一 canonical Runtime step identity）。

冻结架构（68 Gate）要求 identity authority 完全分离，禁止把 presentation name 当
runtime identity：

```text
DATASET_GROUNDING_SEMANTICS = CANONICAL_STEP_IDENTITY
PLAN_STEP_ID_AUTHORITY = PlanStep.step_id
STEP_DISPLAY_NAME_AUTHORITY = PlanStep.title -> StepState.name
EPISODE_OBSERVATION_IDENTITY = NONE
JOURNAL_CANONICAL_STEP_IDENTITY = RuntimeEvent.step_id
TASK_PREFIX_NORMALIZATION = PlanCompiler._specialist_step_id: task_id -> task-<task_id>
```

``EpisodicStepIdentity`` 是 Dataset symbolic ``RequiredObservedStep.step_ref`` 的 typed
解析结果：严格校验后归一化为 canonical Runtime ``PlanStep.step_id``。``EpisodicStepIdentityAdapter``
是唯一 owner：任何 evaluator / runner 都不得散落 ``"task-" + ref`` 字符串拼接、不得做
``.contains()``、canonical-text 搜索、display-name fallback、SQLite content lookup 或
created_at 猜测。

语义与 LocalAgent ``PlanCompiler._specialist_step_id()``（``<task_id> -> task-<task_id>``）
严格一致；AgentEvalOps 不 import LocalAgent 内部实现，而是维护 frozen typed wire/identity
contract，由 cross-repo contract test 验证两边一致（``episodic_step_identity.py`` 的
``TASK_ID_PREFIX`` 常量即该 contract）。

fail-closed：unknown/invalid symbolic ref 抛 ``EpisodicStepIdentityError``（绝不做模糊
匹配或 fallback）。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: 与 LocalAgent ``PlanCompiler._specialist_step_id`` 一致的固定 prefix（frozen wire
#: contract；禁止在 evaluator 中散落 ``"task-" + x``）。
TASK_ID_PREFIX: str = "task-"

#: frozen typed wire/identity normalization contract（cross-repo contract test 用）。
EPISODIC_STEP_IDENTITY_NORMALIZATION_CONTRACT: str = (
    "<symbolic step_ref> -> task-<symbolic step_ref> == PlanCompiler._specialist_step_id()"
)

#: LocalAgent planner task-id wire 格式：小写 snake_case 标识符（与 Dataset
#: ``RequiredObservedStep.step_ref`` / ``_require_wire_id`` 一致）。
_TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class EpisodicStepIdentityError(ValueError):
    """symbolic step ref 无法解析为 canonical Runtime step identity（fail closed）。"""


class EpisodicStepIdentityNormalizationStatus(StrEnum):
    """typed normalization 结果状态。"""

    NORMALIZED = "NORMALIZED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class EpisodicStepIdentity:
    """Dataset symbolic step_ref 的 typed 解析结果（canonical Runtime step identity）。

    - ``symbolic_ref``：Dataset ``RequiredObservedStep.step_ref``（planner task-id）。
    - ``step_id``：canonical Runtime ``PlanStep.step_id`` / Journal ``RuntimeEvent.step_id``
      （``task-<symbolic_ref>``）。
    - ``status``：NORMALIZED / INVALID。
    """

    symbolic_ref: str
    step_id: str
    status: EpisodicStepIdentityNormalizationStatus

    def __post_init__(self) -> None:
        from app.core.evaluation.immutable import require_text

        require_text(self.symbolic_ref, "symbolic_ref")
        require_text(self.step_id, "step_id")

    @property
    def normalized(self) -> bool:
        """Return the computed property value."""
        return self.status is EpisodicStepIdentityNormalizationStatus.NORMALIZED


class EpisodicStepIdentityAdapter:
    """Dataset symbolic step ref -> canonical Runtime step identity 的单一 owner。

    只做 deterministic typed normalization；所有 normalization 必须经过本 adapter。
    """

    def normalize(self, symbolic_ref: str) -> EpisodicStepIdentity:
        """Normalize a Dataset symbolic step_ref to the canonical Runtime step_id.

        Args:
            symbolic_ref: Dataset ``RequiredObservedStep.step_ref``（planner task-id）。

        Returns:
            NORMALIZED typed identity；step_id == ``task-<symbolic_ref>``。

        Raises:
            EpisodicStepIdentityError: ref 不符合 planner task-id 格式（fail closed，
                不模糊匹配、不 fallback）。
        """
        if not isinstance(symbolic_ref, str) or not symbolic_ref.strip():
            raise EpisodicStepIdentityError("step_ref must be a non-empty string")
        if _TASK_ID_PATTERN.fullmatch(symbolic_ref) is None:
            raise EpisodicStepIdentityError(
                f"invalid symbolic step_ref {symbolic_ref!r}: must match planner task-id wire format ([a-z][a-z0-9_]*)"
            )
        return EpisodicStepIdentity(
            symbolic_ref=symbolic_ref,
            step_id=f"{TASK_ID_PREFIX}{symbolic_ref}",
            status=EpisodicStepIdentityNormalizationStatus.NORMALIZED,
        )


__all__ = [
    "EPISODIC_STEP_IDENTITY_NORMALIZATION_CONTRACT",
    "EpisodicStepIdentity",
    "EpisodicStepIdentityAdapter",
    "EpisodicStepIdentityError",
    "EpisodicStepIdentityNormalizationStatus",
    "TASK_ID_PREFIX",
]
