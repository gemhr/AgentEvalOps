"""WP6-E symbolic Episode identity resolver。

``EpisodicIdentityResolver`` 只通过 Dataset 声明 + receipt 的 authoritative mapping
解析 symbolic ``episode_ref`` -> runtime ``memory_id``：

- RUN_FORMED：episode_ref -> Dataset origin run_id -> actual runtime run UUID ->
  formation receipt -> memory_id；
- DATASET_CONTROLLED_INITIAL_FIXTURE：episode_ref/fixture_ref -> fixture
  installation receipt -> memory_id。

Resolver 禁止内容相似度匹配、canonical text 比较、按 SQLite/created_at 猜“最像的
一条”。identity evidence missing 由 evaluator 映射为 ``BLOCKED / EVIDENCE_CAPTURE``，
绝不把 expected identity 当作“未选择”的 Retrieval Miss。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.evaluation.episodic_dataset import (
    EpisodicEpisodeOriginKind,
    EpisodicScenario,
)
from app.core.evaluation.episodic_evidence import (
    EpisodicFixtureReceiptEvidence,
    EpisodicFormationReceiptEvidence,
)


class IdentityResolutionStatus(StrEnum):
    """symbolic episode identity 的解析状态。

    - RESOLVED：由 receipt 的 authoritative mapping 解析到 runtime memory_id。
    - MISSING_FORMATION_RECEIPT：expected RUN_FORMED identity 没有可解析的
      formation receipt（BLOCKED / EVIDENCE_CAPTURE）。
    - MISSING_FIXTURE_RECEIPT：expected fixture identity 没有安装 receipt。
    - NOT_DECLARED：Dataset 未声明该 episode binding（不应出现；防御性）。
    """

    RESOLVED = "RESOLVED"
    MISSING_FORMATION_RECEIPT = "MISSING_FORMATION_RECEIPT"
    MISSING_FIXTURE_RECEIPT = "MISSING_FIXTURE_RECEIPT"
    NOT_DECLARED = "NOT_DECLARED"


@dataclass(frozen=True, slots=True)
class EpisodicIdentityResolution:
    """一个 symbolic episode_ref 的解析结果。"""

    episode_ref: str
    origin_kind: EpisodicEpisodeOriginKind
    status: IdentityResolutionStatus
    memory_id: str | None = None
    source: str | None = None

    @property
    def resolved(self) -> bool:
        """Return the computed property value."""
        return self.status is IdentityResolutionStatus.RESOLVED and self.memory_id is not None


@dataclass(frozen=True, slots=True)
class EpisodicIdentityMap:
    """scenario 的 symbolic episode_ref -> runtime memory_id 映射（由 resolver 构建）。"""

    resolutions: tuple[EpisodicIdentityResolution, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolutions", tuple(self.resolutions))
        refs = [item.episode_ref for item in self.resolutions]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate episode_ref in identity map")

    def memory_id_for(self, episode_ref: str) -> str | None:
        """Implement the ``memory_id_for`` contract (typed, fail-closed)."""
        for resolution in self.resolutions:
            if resolution.episode_ref == episode_ref:
                return resolution.memory_id
        return None

    def resolution_for(self, episode_ref: str) -> EpisodicIdentityResolution | None:
        """Implement the ``resolution_for`` contract (typed, fail-closed)."""
        for resolution in self.resolutions:
            if resolution.episode_ref == episode_ref:
                return resolution
        return None

    def status_for(self, episode_ref: str) -> IdentityResolutionStatus | None:
        """Implement the ``status_for`` contract (typed, fail-closed)."""
        resolution = self.resolution_for(episode_ref)
        return resolution.status if resolution is not None else None

    def unresolved_refs(self) -> tuple[str, ...]:
        """Implement the ``unresolved_refs`` contract (typed, fail-closed)."""
        return tuple(
            resolution.episode_ref
            for resolution in self.resolutions
            if resolution.status is not IdentityResolutionStatus.RESOLVED
        )


class EpisodicIdentityResolver:
    """只基于 Dataset 声明 + receipt 的 authoritative mapping 解析 symbolic identity。"""

    def resolve(
        self,
        scenario: EpisodicScenario,
        *,
        formation_receipt_by_run_id: Mapping[str, EpisodicFormationReceiptEvidence],
        fixture_receipt_by_ref: Mapping[str, EpisodicFixtureReceiptEvidence],
    ) -> EpisodicIdentityMap:
        """Resolve the symbolic episode identities from receipts (no content inference)."""
        resolutions: list[EpisodicIdentityResolution] = []
        """Resolve the symbolic episode identities from receipts (no content inference)."""
        for binding in scenario.episodes:
            if binding.origin_kind is EpisodicEpisodeOriginKind.RUN_FORMED:
                run_id = binding.origin_run_id
                assert run_id is not None
                receipt = formation_receipt_by_run_id.get(run_id)
                if receipt is None or receipt.memory_id is None:
                    resolutions.append(
                        EpisodicIdentityResolution(
                            episode_ref=binding.episode_ref,
                            origin_kind=binding.origin_kind,
                            status=IdentityResolutionStatus.MISSING_FORMATION_RECEIPT,
                        )
                    )
                    continue
                resolutions.append(
                    EpisodicIdentityResolution(
                        episode_ref=binding.episode_ref,
                        origin_kind=binding.origin_kind,
                        status=IdentityResolutionStatus.RESOLVED,
                        memory_id=receipt.memory_id,
                        source=f"formation_receipt:{run_id}",
                    )
                )
            else:
                receipt = fixture_receipt_by_ref.get(binding.episode_ref)
                if receipt is None or receipt.memory_id is None:
                    resolutions.append(
                        EpisodicIdentityResolution(
                            episode_ref=binding.episode_ref,
                            origin_kind=binding.origin_kind,
                            status=IdentityResolutionStatus.MISSING_FIXTURE_RECEIPT,
                        )
                    )
                    continue
                resolutions.append(
                    EpisodicIdentityResolution(
                        episode_ref=binding.episode_ref,
                        origin_kind=binding.origin_kind,
                        status=IdentityResolutionStatus.RESOLVED,
                        memory_id=receipt.memory_id,
                        source="fixture_receipt",
                    )
                )
        return EpisodicIdentityMap(resolutions=tuple(resolutions))


__all__ = [
    "EpisodicIdentityMap",
    "EpisodicIdentityResolution",
    "EpisodicIdentityResolver",
    "IdentityResolutionStatus",
]
