"""WP6-E typed step identity normalization owner 契约测试（68 Gate frozen contract）。

要求（68 Gate / WP6-E-D-G5-R1 §21）：

1. release_list -> task-release_list
2. replication_check -> task-replication_check
3. deploy_answer -> task-deploy_answer
4. deploy_probe -> task-deploy_probe
5. recovery_summary -> task-recovery_summary
6. audit_summary -> task-audit_summary
7. env_status -> task-env_status
8. invalid symbolic ref fail closed
9. normalization 只有单一 owner（无散落 ``"task-" + x``）
10. evaluator 没有 display-name fallback
"""

# ruff: noqa: D101, D105, D415

import pytest

from app.core.evaluation.episodic_evaluators import _runtime_step_facts
from app.core.evaluation.episodic_evidence import EpisodicRunEvidence, RunExecutionStatus
from app.core.evaluation.episodic_step_identity import (
    EPISODIC_STEP_IDENTITY_NORMALIZATION_CONTRACT,
    TASK_ID_PREFIX,
    EpisodicStepIdentityAdapter,
    EpisodicStepIdentityError,
    EpisodicStepIdentityNormalizationStatus,
)
from app.core.evaluation.stateful_journal import JournalStepFact, JournalStepFacts

ADAPTER = EpisodicStepIdentityAdapter()


def _run_evidence(**overrides):
    return EpisodicRunEvidence(
        scenario_id="e01_meaningful_success_forms_episode",
        case_code="E01",
        dataset_run_id="run_a",
        actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174500",
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1-7: frozen symbolic ref -> canonical step_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbolic", "canonical"),
    [
        ("release_list", "task-release_list"),
        ("replication_check", "task-replication_check"),
        ("deploy_answer", "task-deploy_answer"),
        ("deploy_probe", "task-deploy_probe"),
        ("recovery_summary", "task-recovery_summary"),
        ("audit_summary", "task-audit_summary"),
        ("env_status", "task-env_status"),
        ("rollback_plan", "task-rollback_plan"),
        ("config_check", "task-config_check"),
        ("audit_list", "task-audit_list"),
    ],
)
def test_symbolic_ref_normalizes_to_canonical_step_id(symbolic, canonical):
    identity = ADAPTER.normalize(symbolic)
    assert identity.symbolic_ref == symbolic
    assert identity.step_id == canonical
    assert identity.status is EpisodicStepIdentityNormalizationStatus.NORMALIZED
    assert identity.normalized is True
    assert canonical.startswith(TASK_ID_PREFIX)


# ---------------------------------------------------------------------------
# 8: invalid symbolic ref fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "  ",
        None,
        "Release_List",
        "release-list",
        "release list",
        "release list!",
        "1release_list",
        "task-release_list",
        "release_list ",
        " release_list",
    ],
)
def test_invalid_symbolic_ref_fails_closed(invalid):
    with pytest.raises(EpisodicStepIdentityError):
        ADAPTER.normalize(invalid)


# ---------------------------------------------------------------------------
# 9: normalization 只有单一 owner；cross-repo contract 与 PlanCompiler 一致
# ---------------------------------------------------------------------------


def test_normalization_single_owner_contract_matches_plan_compiler_semantics():
    # PlanCompiler._specialist_step_id: <task_id> -> task-<task_id>（68 Gate frozen）。
    for symbolic in ("release_list", "replication_check", "deploy_answer"):
        identity = ADAPTER.normalize(symbolic)
        assert identity.step_id == f"{TASK_ID_PREFIX}{symbolic}"
        # 显式禁止 evaluator 内联拼接：adapter 是唯一 owner。
        assert EPISODIC_STEP_IDENTITY_NORMALIZATION_CONTRACT
        assert TASK_ID_PREFIX == "task-"


def test_adapter_is_used_by_evaluator_for_step_facts():
    # evaluator 消费 runtime step facts 时必须通过 adapter 的 canonical step_id；
    # 直接构造 step_facts 后 _runtime_step_facts 只按 canonical step_id 检索。
    facts = JournalStepFacts(
        run_id="run",
        facts=(
            JournalStepFact("run", "e1", "STEP_STARTED", "task-release_list", None),
            JournalStepFact("run", "e2", "STEP_COMPLETED", "task-release_list", "SUCCEEDED"),
        ),
    )
    step_facts, source = _runtime_step_facts(_run_evidence(step_facts=facts))
    assert source == "journal"
    assert step_facts == {"task-release_list": "SUCCEEDED"}


# ---------------------------------------------------------------------------
# 10: evaluator 没有 display-name fallback
# ---------------------------------------------------------------------------


def test_evaluator_step_facts_never_uses_display_name():
    # 只有 display name（EpisodeObservation.name / StepState.name）的 evidence 绝不
    # 产生 canonical step_id：_runtime_step_facts 只消费 JournalStepFacts / receipt
    # step_names（receipt step_names 是 display-name 对，不是 identity authority）。
    record = _run_evidence()
    step_facts, source = _runtime_step_facts(record)
    assert step_facts == {}
    assert source is None


def test_runtime_receipt_step_names_are_display_presence_not_identity_authority():
    # receipt step_names 是 display-name/status 对（target 现有 wire）；identity
    # authority 必须是 Journal RuntimeEvent.step_id。这里证明：即使 receipt 有
    # display name，也不等于 canonical step_id（display name 不是 task-<ref>）。
    from app.core.evaluation.episodic_evidence import EpisodicRuntimeReceiptEvidence

    receipt = EpisodicRuntimeReceiptEvidence(
        run_id="run",
        plan_goal=None,
        step_names=("执行专业任务",),
        step_statuses=("SUCCEEDED",),
        terminal_status="SUCCEEDED",
        stop_reason="COMPLETED",
        delivery_status="DELIVERED",
        formed_memory_id=None,
        formation_outcome=None,
        canonical_text_sha256=None,
    )
    record = _run_evidence(runtime_receipt=receipt)
    step_facts, source = _runtime_step_facts(record)
    # receipt step_names 是 display-name；其 key 不是 canonical task-<ref>，
    # 因此 identity 检索（task-release_list）命中不到 -> 不能作为 identity authority。
    assert source == "runtime_receipt"
    assert "task-release_list" not in step_facts
    assert "执行专业任务" in step_facts
