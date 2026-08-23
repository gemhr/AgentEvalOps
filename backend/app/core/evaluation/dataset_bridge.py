# ruff: noqa: D415

"""把 file dataset 的 Phase1 generation authority 投影到既有 runtime catalog。"""

from __future__ import annotations

from datetime import datetime

from app.core.evaluation.catalog import DatasetVersion, TestCaseVersion
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.references import CaseVersionRef


def bridge_dataset_to_catalog(
    dataset: EvaluationDataset,
    *,
    created_at: datetime,
) -> tuple[DatasetVersion, dict[CaseVersionRef, TestCaseVersion]]:
    """以 dataset version 作为运行 case version，保留 generation ground truth authority。"""
    cases: dict[CaseVersionRef, TestCaseVersion] = {}
    refs: list[CaseVersionRef] = []
    for item in dataset.cases:
        ref = CaseVersionRef(item.case_id, dataset.version)
        reference_answer = None
        if item.ground_truth.generation is not None:
            reference_answer = item.ground_truth.generation.reference_answer
        metadata: dict[str, object] = {
            **item.metadata,
            "generation_reference_authority": "ground_truth.generation.reference_answer",
        }
        rag_ground_truth: dict[str, object] = {}
        if item.ground_truth.retrieval is not None:
            rag_ground_truth["retrieval"] = item.ground_truth.retrieval.model_dump(mode="json")
        if item.ground_truth.ranking is not None:
            rag_ground_truth["ranking"] = item.ground_truth.ranking.model_dump(mode="json")
        if rag_ground_truth:
            metadata["rag_ground_truth"] = rag_ground_truth
        if item.ground_truth.security is not None:
            metadata["security_ground_truth"] = item.ground_truth.security.model_dump(mode="json")
        cases[ref] = TestCaseVersion(
            case_id=item.case_id,
            version=dataset.version,
            name=item.name,
            input_payload=item.input,
            expected_output=reference_answer,
            created_at=created_at,
            metadata=metadata,
        )
        refs.append(ref)
    return (
        DatasetVersion(
            dataset_id=dataset.dataset_id,
            version=dataset.version,
            name=dataset.name,
            description=dataset.description,
            created_at=created_at,
            case_version_refs=tuple(refs),
        ),
        cases,
    )


__all__ = ["bridge_dataset_to_catalog"]
