from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.core.evaluation import (
    ArtifactRef,
    AssertionSpec,
    CaseVersionRef,
    DatasetVersion,
    EvidenceRef,
    TestCaseVersion as EvaluationTestCaseVersion,
    VersionRef,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def test_reference_types_are_validated_hashable_and_immutable() -> None:
    version = VersionRef("runtime", "opaque-v1")
    case_ref = CaseVersionRef("case-a", "v1")
    artifact = ArtifactRef("artifact-a", digest="sha256:123", metadata={"nested": {"x": [1, 2]}})
    evidence = EvidenceRef("log", "log-a", media_type="text/plain", schema_version="v1")

    assert hash(case_ref)
    assert version.opaque_value == "opaque-v1"
    assert artifact.metadata["nested"]["x"] == (1, 2)
    assert evidence.identifier == "log-a"
    with pytest.raises(FrozenInstanceError):
        case_ref.version = "v2"  # type: ignore[misc]
    with pytest.raises(TypeError):
        artifact.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        artifact.metadata._values["new"] = "value"  # type: ignore[attr-defined,index]
    with pytest.raises(TypeError):
        artifact.metadata._items = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: VersionRef("", "v1"), "kind"),
        (lambda: VersionRef("runtime", " "), "opaque_value"),
        (lambda: CaseVersionRef("", "v1"), "case_id"),
        (lambda: ArtifactRef(""), "artifact_id"),
        (lambda: EvidenceRef("trace", ""), "identifier"),
    ],
)
def test_reference_types_reject_empty_identity(factory: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()  # type: ignore[operator]


def test_dataset_preserves_order_and_has_identity_equality() -> None:
    refs = [CaseVersionRef("case-b", "v1"), CaseVersionRef("case-a", "v2")]
    metadata = {"owners": ["quality"]}
    dataset = DatasetVersion(
        dataset_id="dataset-a",
        version="v2",
        parent_version="v1",
        name="Core cases",
        case_version_refs=refs,
        tags=["critical", "offline"],
        metadata=metadata,
        created_at=NOW,
    )
    same_identity = DatasetVersion(dataset_id="dataset-a", version="v2", name="Renamed", created_at=NOW)

    refs.reverse()
    metadata["owners"].append("mutated")
    assert dataset.case_version_refs == (CaseVersionRef("case-b", "v1"), CaseVersionRef("case-a", "v2"))
    assert dataset.metadata["owners"] == ("quality",)
    assert dataset == same_identity
    assert hash(dataset) == hash(same_identity)


def test_dataset_rejects_duplicate_cases_and_self_parent() -> None:
    case_ref = CaseVersionRef("case-a", "v1")
    with pytest.raises(ValueError, match="duplicate case_version_ref"):
        DatasetVersion(
            dataset_id="dataset-a",
            version="v1",
            name="Duplicate",
            case_version_refs=[case_ref, case_ref],
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="parent_version"):
        DatasetVersion(
            dataset_id="dataset-a",
            version="v1",
            parent_version="v1",
            name="Self parent",
            created_at=NOW,
        )


def test_test_case_accepts_opaque_json_and_deep_freezes_snapshots() -> None:
    input_payload = {"messages": ["hello", {"weight": 1.5}], "enabled": True}
    expected_output = ["world", {"count": 2}]
    config = {"path": ["answer", "text"]}
    metadata = {"labels": ["smoke"]}
    case = EvaluationTestCaseVersion(
        case_id="case-a",
        version="v1",
        name="Opaque case",
        input_payload=input_payload,
        expected_output=expected_output,
        assertion_specs=[AssertionSpec("assert-a", "structured", config=config)],
        fixture_refs=[ArtifactRef("fixture-a")],
        evidence_refs=[EvidenceRef("observation", "obs-a")],
        tags=["smoke"],
        metadata=metadata,
        created_at=NOW,
    )

    input_payload["messages"].append("mutated")
    expected_output.append("mutated")
    config["path"].append("mutated")
    metadata["labels"].append("mutated")
    assert case.input_payload["messages"] == ("hello", {"weight": 1.5})
    assert case.expected_output == ("world", {"count": 2})
    assert case.assertion_specs[0].config["path"] == ("answer", "text")
    assert case.metadata["labels"] == ("smoke",)
    with pytest.raises(FrozenInstanceError):
        case.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("payload", [{1: "bad-key"}, {"x": object()}, float("nan")])
def test_test_case_rejects_non_json_payload(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        EvaluationTestCaseVersion(
            case_id="case-a",
            version="v1",
            name="Invalid",
            input_payload=payload,  # type: ignore[arg-type]
            created_at=NOW,
        )


def test_catalog_constructs_without_trace() -> None:
    case = EvaluationTestCaseVersion(
        case_id="case-a",
        version="v1",
        name="No trace",
        input_payload="hello",
        expected_output=None,
        created_at=NOW,
    )
    dataset = DatasetVersion(
        dataset_id="dataset-a",
        version="v1",
        name="No trace dataset",
        case_version_refs=[CaseVersionRef(case.case_id, case.version)],
        created_at=NOW,
    )
    assert dataset.case_version_refs[0].case_id == case.case_id
