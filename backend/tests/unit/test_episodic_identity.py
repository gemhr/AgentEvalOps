"""WP6-E symbolic Episode identity resolver 契约测试。"""

# ruff: noqa: D101, D105, D415

from app.core.evaluation.episodic_evidence import (
    EpisodicFixtureReceiptEvidence,
    EpisodicFormationReceiptEvidence,
)
from app.core.evaluation.episodic_identity import (
    EpisodicIdentityResolver,
    IdentityResolutionStatus,
)
from tests.unit.episodic_fixtures import load_dataset, scenario_by_case

DATASET = load_dataset()
RESOLVER = EpisodicIdentityResolver()


def test_run_formed_symbolic_ref_resolves_via_formation_receipt():
    scenario = scenario_by_case(DATASET, "E07")
    run_a_id = "123e4567-e89b-12d3-a456-426614174100"
    run_b_id = "123e4567-e89b-12d3-a456-426614174101"
    formation_by_run = {
        "run_a": EpisodicFormationReceiptEvidence(
            run_id=run_a_id, outcome="CREATED", memory_id="episode-aaaa", lesson_status="ABSENT"
        ),
        "run_b": EpisodicFormationReceiptEvidence(
            run_id=run_b_id, outcome="CREATED", memory_id="episode-bbbb", lesson_status="ABSENT"
        ),
    }
    identity_map = RESOLVER.resolve(scenario, formation_receipt_by_run_id=formation_by_run, fixture_receipt_by_ref={})
    assert identity_map.memory_id_for("run_a_episode") == "episode-aaaa"
    assert identity_map.memory_id_for("run_b_episode") == "episode-bbbb"
    assert identity_map.status_for("run_a_episode") is IdentityResolutionStatus.RESOLVED
    assert identity_map.unresolved_refs() == ()


def test_fixture_symbolic_ref_resolves_via_install_receipt():
    scenario = scenario_by_case(DATASET, "E09")
    fixture_receipts = {
        "foreign_scope_episode": EpisodicFixtureReceiptEvidence(
            fixture_ref="foreign_scope_episode",
            memory_id="episode-fixture1",
            origin_run_id="fixture-origin",
            origin_kind="DATASET_CONTROLLED_INITIAL_FIXTURE",
            memory_scope="orchestration",
        ),
    }
    # E09 的 run_a 是 RUN_FORMED，需要 formation receipt
    formation_by_run = {
        "run_a": EpisodicFormationReceiptEvidence(
            run_id="123e4567-e89b-12d3-a456-426614174102",
            outcome="CREATED",
            memory_id="episode-runa",
            lesson_status="ABSENT",
        )
    }
    identity_map = RESOLVER.resolve(
        scenario, formation_receipt_by_run_id=formation_by_run, fixture_receipt_by_ref=fixture_receipts
    )
    assert identity_map.memory_id_for("foreign_scope_episode") == "episode-fixture1"
    assert identity_map.memory_id_for("run_a_episode") == "episode-runa"


def test_missing_formation_receipt_blocks_not_miss():
    scenario = scenario_by_case(DATASET, "E07")
    identity_map = RESOLVER.resolve(
        scenario,
        formation_receipt_by_run_id={
            "run_a": EpisodicFormationReceiptEvidence(
                run_id="123e4567-e89b-12d3-a456-426614174103",
                outcome="CREATED",
                memory_id="episode-aaaa",
                lesson_status="ABSENT",
            )
        },
        fixture_receipt_by_ref={},
    )
    # run_b_episode 缺 formation receipt -> MISSING_FORMATION_RECEIPT（BLOCKED），
    # 绝不是未选择。
    assert identity_map.status_for("run_a_episode") is IdentityResolutionStatus.RESOLVED
    assert identity_map.status_for("run_b_episode") is IdentityResolutionStatus.MISSING_FORMATION_RECEIPT
    assert identity_map.unresolved_refs() == ("run_b_episode",)


def test_missing_fixture_receipt_blocks():
    scenario = scenario_by_case(DATASET, "E09")
    identity_map = RESOLVER.resolve(
        scenario,
        formation_receipt_by_run_id={
            "run_a": EpisodicFormationReceiptEvidence(
                run_id="123e4567-e89b-12d3-a456-426614174104",
                outcome="CREATED",
                memory_id="episode-runa",
                lesson_status="ABSENT",
            )
        },
        fixture_receipt_by_ref={},
    )
    assert identity_map.status_for("foreign_scope_episode") is IdentityResolutionStatus.MISSING_FIXTURE_RECEIPT


def test_no_content_based_fallback_and_duplicate_rejection():
    scenario = scenario_by_case(DATASET, "E07")
    # formation receipt 缺 memory_id -> 解析失败，绝不按 canonical text 反查。
    identity_map = RESOLVER.resolve(
        scenario,
        formation_receipt_by_run_id={
            "run_a": EpisodicFormationReceiptEvidence(
                run_id="123e4567-e89b-12d3-a456-426614174105",
                outcome="CREATED",
                memory_id=None,
                lesson_status="ABSENT",
            ),
            "run_b": EpisodicFormationReceiptEvidence(
                run_id="123e4567-e89b-12d3-a456-426614174106",
                outcome="CREATED",
                memory_id="episode-bbbb",
                lesson_status="ABSENT",
            ),
        },
        fixture_receipt_by_ref={},
    )
    assert identity_map.memory_id_for("run_a_episode") is None
    assert identity_map.status_for("run_a_episode") is IdentityResolutionStatus.MISSING_FORMATION_RECEIPT
