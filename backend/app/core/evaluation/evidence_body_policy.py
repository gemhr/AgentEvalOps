"""被冻结的过程证据正文策略。"""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

from enum import StrEnum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator

from app.core.evaluation.references import VersionRef

EVIDENCE_BODY_POLICY_SCHEMA = "evidence_body_policy.v1"
APPROVED_BODY_FIELDS = frozenset({"final_answer.body"})


class EvidenceBodyMode(StrEnum):
    METADATA_ONLY = "METADATA_ONLY"
    APPROVED_SAFE_SNAPSHOT = "APPROVED_SAFE_SNAPSHOT"


class EvidenceBodyPolicyV1(BaseModel):
    """Strict immutable policy serialized inside a frozen Suite snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: StrictStr = EVIDENCE_BODY_POLICY_SCHEMA
    mode: EvidenceBodyMode = EvidenceBodyMode.METADATA_ONLY
    policy_ref: VersionRef | None = None
    approved_fields: tuple[StrictStr, ...] = ()

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: object) -> object:
        return EvidenceBodyMode(value) if isinstance(value, str) else value

    @field_validator("approved_fields", mode="before")
    @classmethod
    def _approved_fields(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("policy_ref", mode="before")
    @classmethod
    def _version_ref(cls, value: object) -> object:
        if isinstance(value, Mapping):
            if set(value) != {"kind", "opaque_value"}:
                raise ValueError("invalid policy_ref")
            return VersionRef(value["kind"], value["opaque_value"])
        return value

    @model_validator(mode="after")
    def _policy_contract(self) -> "EvidenceBodyPolicyV1":
        if self.schema_version != EVIDENCE_BODY_POLICY_SCHEMA:
            raise ValueError("unsupported evidence body policy schema")
        fields = tuple(self.approved_fields)
        if len(fields) != len(set(fields)):
            raise ValueError("duplicate approved body field")
        if self.mode is EvidenceBodyMode.METADATA_ONLY:
            if self.policy_ref is not None or fields:
                raise ValueError("METADATA_ONLY cannot authorize body fields")
        else:
            if self.policy_ref is None or self.policy_ref.kind != "evidence_body_policy":
                raise ValueError("APPROVED_SAFE_SNAPSHOT requires trusted evidence_body_policy VersionRef")
            if not fields or set(fields) - APPROVED_BODY_FIELDS:
                raise ValueError("unsupported approved body field")
        return self

    @property
    def identity_ref(self) -> str | None:
        if self.policy_ref is None:
            return None
        return f"{self.policy_ref.kind}:{self.policy_ref.opaque_value}"


def parse_evidence_body_policy(suite_snapshot: object) -> EvidenceBodyPolicyV1:
    """从 frozen Suite metadata 解析策略；缺省为 metadata-only。"""
    if not isinstance(suite_snapshot, Mapping):
        raise ValueError("invalid frozen suite snapshot")
    metadata = suite_snapshot.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("invalid frozen suite metadata")
    raw = metadata.get("evidence_body_policy")
    if raw is None:
        return EvidenceBodyPolicyV1()
    if not isinstance(raw, Mapping):
        raise ValueError("invalid evidence_body_policy")
    try:
        return EvidenceBodyPolicyV1.model_validate(dict(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid frozen evidence_body_policy.v1") from exc
