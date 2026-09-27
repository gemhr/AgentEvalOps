"""无需外部服务的 deterministic FixtureExecutionTarget。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
import hashlib
import json
from datetime import datetime
from types import MappingProxyType

from app.core.evaluation.execution import (
    FIXTURE_TARGET_KIND,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTargetRef,
    OutcomeKind,
)
from app.core.evaluation.immutable import FrozenDict, json_compatible
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, freeze_metadata


@dataclass(frozen=True, slots=True)
class FixtureExecution:
    """某个 Case 的预配置 terminal outcome template。"""

    kind: OutcomeKind
    started_at: datetime
    finished_at: datetime
    output_artifact_ref: ArtifactRef | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    error_category: str | None = None
    reason: str | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
        ExecutionOutcome(
            request_id="fixture-contract-validation",
            kind=self.kind,
            started_at=self.started_at,
            finished_at=self.finished_at,
            output_artifact_ref=self.output_artifact_ref,
            evidence_refs=self.evidence_refs,
            error_category=self.error_category,
            reason=self.reason,
            metadata=self.metadata,
        )


class FixtureExecutionTarget:
    """按 CaseVersionRef 查找独立 actual fixture 的无 I/O adapter。"""

    __slots__ = ("_fixtures", "_target_ref")

    def __init__(
        self,
        target_ref: ExecutionTargetRef,
        fixtures: Mapping[CaseVersionRef, FixtureExecution],
    ) -> None:
        if target_ref.target_kind != FIXTURE_TARGET_KIND:
            raise ValueError(f"unsupported target kind for fixture adapter: {target_ref.target_kind}")
        self._target_ref = target_ref
        self._fixtures = MappingProxyType(dict(fixtures))

    @property
    def target_ref(self) -> ExecutionTargetRef:
        """返回不可变 Target snapshot。"""
        return self._target_ref

    @property
    def subject_ref(self) -> dict[str, object]:
        """创建 Run 时冻结实际 fixture 模板内容身份，排除执行时钟。"""
        templates = []
        for ref, fixture in sorted(self._fixtures.items()):
            artifact = fixture.output_artifact_ref
            content = {
                "kind": fixture.kind.value,
                "output_artifact_ref": None if artifact is None else {
                    item.name: json_compatible(getattr(artifact, item.name)) for item in fields(artifact)
                },
                "evidence_refs": [
                    {item.name: json_compatible(getattr(ref, item.name)) for item in fields(ref)}
                    for ref in fixture.evidence_refs
                ],
                "error_category": fixture.error_category,
                "reason": fixture.reason,
                "metadata": json_compatible(fixture.metadata),
            }
            templates.append({"case_id": ref.case_id, "version": ref.version, "content": json_compatible(content)})
        encoded = json.dumps(templates, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"fixture_target_identity": self._target_ref.target_id,
                "fixture_content_identity": "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """确定性地把 Case 映射为预配置 terminal outcome。"""
        try:
            fixture = self._fixtures[request.case_ref]
        except KeyError as error:
            raise LookupError(f"no fixture configured for case {request.case_ref!r}") from error

        metadata = dict(fixture.metadata)
        metadata["idempotency_key"] = request.idempotency_key
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=fixture.kind,
            started_at=fixture.started_at,
            finished_at=fixture.finished_at,
            output_artifact_ref=fixture.output_artifact_ref,
            evidence_refs=fixture.evidence_refs,
            error_category=fixture.error_category,
            reason=fixture.reason,
            metadata=metadata,
        )
