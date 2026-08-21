"""Final answer evidence v1 的 strict consumer DTO 与 EvidenceRef 映射。"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator

from app.core.evaluation.references import EvidenceRef

FINAL_ANSWER_EVIDENCE_SCHEMA_VERSION = "final-answer-evidence.v1"
FINAL_ANSWER_EVIDENCE_KIND = "final_answer"
FINAL_ANSWER_MEDIA_TYPE = "application/vnd.localagent.final-answer+json"
FINAL_ANSWER_EVIDENCE_REF_SCHEMA_VERSION = "v1"
FINAL_ANSWER_CONTENT_MEDIA_TYPE = "text/plain; charset=utf-8"
FINAL_ANSWER_MAX_BYTES = 64 * 1024

_EVIDENCE_ID = re.compile(r"^final-answer://([A-Za-z0-9-]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FinalAnswerEvidenceV1(BaseModel):
    """只接受由 LocalAgent delivered output 生成的 v1 final answer evidence。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr
    evidence_id: StrictStr
    run_id: StrictStr
    attempt_id: StrictStr
    media_type: StrictStr
    content_sha256: StrictStr
    content: StrictStr

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != FINAL_ANSWER_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if value != FINAL_ANSWER_CONTENT_MEDIA_TYPE:
            raise ValueError("unsupported media_type")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _digest_shape(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid content_sha256")
        return value

    @field_validator("content")
    @classmethod
    def _content_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > FINAL_ANSWER_MAX_BYTES:
            raise ValueError("content exceeds UTF-8 byte bound")
        return value

    @model_validator(mode="after")
    def _validate_identity_and_digest(self) -> "FinalAnswerEvidenceV1":
        match = _EVIDENCE_ID.fullmatch(self.evidence_id)
        if match is None or match.group(1) != self.run_id:
            raise ValueError("evidence_id does not match run_id")
        if self.attempt_id != self.run_id:
            raise ValueError("attempt_id must equal run_id")
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if digest != self.content_sha256:
            raise ValueError("content_sha256 does not match content")
        return self


def build_final_answer_evidence(artifact: FinalAnswerEvidenceV1) -> EvidenceRef:
    """映射为既有 inline EvidenceRef，不新建 Artifact Store 或 DB table。"""
    return EvidenceRef(
        kind=FINAL_ANSWER_EVIDENCE_KIND,
        identifier=artifact.evidence_id,
        media_type=FINAL_ANSWER_MEDIA_TYPE,
        schema_version=FINAL_ANSWER_EVIDENCE_REF_SCHEMA_VERSION,
        metadata={"payload": artifact.model_dump(mode="json")},
    )


__all__ = [
    "FINAL_ANSWER_CONTENT_MEDIA_TYPE",
    "FINAL_ANSWER_EVIDENCE_KIND",
    "FINAL_ANSWER_EVIDENCE_REF_SCHEMA_VERSION",
    "FINAL_ANSWER_EVIDENCE_SCHEMA_VERSION",
    "FINAL_ANSWER_MAX_BYTES",
    "FINAL_ANSWER_MEDIA_TYPE",
    "FinalAnswerEvidenceV1",
    "build_final_answer_evidence",
]
