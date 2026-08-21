"""FinalAnswerEvidenceV1 strict parser / persistence mapping tests."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.evaluation.generation_evidence import (
    FINAL_ANSWER_CONTENT_MEDIA_TYPE,
    FINAL_ANSWER_EVIDENCE_KIND,
    FINAL_ANSWER_MAX_BYTES,
    FinalAnswerEvidenceV1,
    build_final_answer_evidence,
)
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    _evidence,
    _evidence_from,
)

RUN_ID = "11111111-1111-4111-8111-111111111111"


def evidence_payload(content: str = "answer-v2") -> dict[str, str]:
    return {
        "schema_version": "final-answer-evidence.v1",
        "evidence_id": f"final-answer://{RUN_ID}",
        "run_id": RUN_ID,
        "attempt_id": RUN_ID,
        "media_type": FINAL_ANSWER_CONTENT_MEDIA_TYPE,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }


def test_final_answer_evidence_parses_and_maps_to_inline_evidence_ref() -> None:
    artifact = FinalAnswerEvidenceV1.model_validate(evidence_payload())
    ref = build_final_answer_evidence(artifact)

    assert ref.kind == FINAL_ANSWER_EVIDENCE_KIND
    assert ref.identifier == f"final-answer://{RUN_ID}"
    assert ref.metadata["payload"]["content"] == "answer-v2"
    assert _evidence_from(_evidence(ref)) == ref


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version="final-answer-evidence.v2"),
        lambda value: value.update(run_id="22222222-2222-4222-8222-222222222222"),
        lambda value: value.update(attempt_id="22222222-2222-4222-8222-222222222222"),
        lambda value: value.update(evidence_id="final-answer://other"),
        lambda value: value.update(content_sha256="0" * 64),
        lambda value: value.update(media_type="text/html"),
        lambda value: value.update(content="x" * (FINAL_ANSWER_MAX_BYTES + 1)),
        lambda value: value.update(unexpected="forbidden"),
    ],
    ids=[
        "schema-version",
        "run-id",
        "attempt-id",
        "evidence-id",
        "digest",
        "media-type",
        "content-over-bound",
        "unknown-field",
    ],
)
def test_final_answer_evidence_rejects_malformed_payload(mutate) -> None:
    payload = deepcopy(evidence_payload())
    mutate(payload)
    with pytest.raises(ValidationError):
        FinalAnswerEvidenceV1.model_validate(payload)
