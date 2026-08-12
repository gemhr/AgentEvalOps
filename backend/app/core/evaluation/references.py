"""Evaluation Domain 的 runtime-neutral 引用类型。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.evaluation.immutable import FrozenDict, JsonValue, freeze_json, require_text


@dataclass(frozen=True, slots=True)
class VersionRef:
    """引用一种尚未冻结内部结构的版本主体。"""

    kind: str
    opaque_value: str

    def __post_init__(self) -> None:
        require_text(self.kind, "kind")
        require_text(self.opaque_value, "opaque_value")


@dataclass(frozen=True, slots=True, order=True)
class CaseVersionRef:
    """TestCase 某个不可变版本的 typed reference。"""

    case_id: str
    version: str

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.version, "version")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """引用执行产物，而不在 Domain 中嵌入大 payload。"""

    artifact_id: str
    digest: str | None = None
    media_type: str | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.artifact_id, "artifact_id")
        if self.digest is not None:
            require_text(self.digest, "digest")
        if self.media_type is not None:
            require_text(self.media_type, "media_type")
        object.__setattr__(self, "metadata", freeze_json(self.metadata))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """引用 Trace、日志、文件或其他通用评价证据。"""

    kind: str
    identifier: str
    media_type: str | None = None
    schema_version: str | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.kind, "kind")
        require_text(self.identifier, "identifier")
        if self.media_type is not None:
            require_text(self.media_type, "media_type")
        if self.schema_version is not None:
            require_text(self.schema_version, "schema_version")
        object.__setattr__(self, "metadata", freeze_json(self.metadata))


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """Suite 对未来 ExecutionTarget 的通用能力要求。"""

    identifier: str

    def __post_init__(self) -> None:
        require_text(self.identifier, "identifier")


def freeze_metadata(value: JsonValue) -> FrozenDict:
    """冻结并确保 metadata 是 JSON object。"""
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenDict):
        raise TypeError("metadata must be a JSON object")
    return frozen
