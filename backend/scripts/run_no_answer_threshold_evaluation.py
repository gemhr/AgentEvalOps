#!/usr/bin/env python
"""运行 WP4 evaluation-only No-Answer threshold calibration（不执行 retrieval）。"""

# ruff: noqa: D103, D415

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--rrf-evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # argparse --help 在此之前退出；避免 CLI help 导入 retrieval/model provider dependency。
    from app.core.evaluation.dataset import AnswerabilitySplit, load_dataset
    from app.core.evaluation.no_answer import RrfEvidenceEnvelope
    from app.services.evaluation.no_answer_threshold import (
        acceptance_gate_v2,
        build_evaluation_context,
        calibrate,
        evaluate,
        privacy_safe_serialization,
        signals_for_split,
        validate_experiment_evidence,
    )

    dataset = load_dataset(args.dataset)
    evidence = RrfEvidenceEnvelope.model_validate(_load_object(args.rrf_evidence))
    validated = validate_experiment_evidence(dataset, evidence)
    if not privacy_safe_serialization(evidence, ()):
        raise ValueError("RRF evidence privacy validation failed")

    by_split = {
        split: [
            case
            for case in dataset.cases
            if case.ground_truth.answerability is not None
            and case.ground_truth.answerability.split == split
        ]
        for split in AnswerabilitySplit
    }
    calibration = calibrate(
        calibration_cases=by_split[AnswerabilitySplit.CALIBRATION],
        calibration_signals=signals_for_split(validated, AnswerabilitySplit.CALIBRATION),
        validated_experiment=validated,
    )
    evaluation_context = build_evaluation_context(validated)
    evaluation = evaluate(
        locked_policy=calibration.locked_policy,
        evaluation_context=evaluation_context,
        evaluation_cases=by_split[AnswerabilitySplit.EVALUATION],
        evaluation_signals=signals_for_split(validated, AnswerabilitySplit.EVALUATION),
    )
    gate = acceptance_gate_v2(
        evaluation=evaluation,
        dataset=dataset,
        evidence=evidence,
        locked_policy=calibration.locked_policy,
        evaluation_context=evaluation_context,
    )
    report = {
        "report_schema_version": "no-answer-threshold-report.v2",
        "capability": "REAL_CALIBRATION_EVALUATION",
        "dataset": {
            "dataset_id": validated.dataset_id,
            "version": validated.dataset_version,
            "digest": validated.dataset_digest,
            "coverage": validated.coverage,
        },
        "rrf_evidence": {
            "schema_version": evidence.schema_version,
            "digest": validated.evidence_digest,
            "config": validated.rrf_config.model_dump(mode="json"),
            "ce_used": evidence.ce_used,
            "new_model_used": evidence.new_model_used,
            "runtime_read_only": evidence.runtime_read_only,
        },
        "calibration": calibration.model_dump(mode="json"),
        "evaluation_context": evaluation_context.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
        "diagnostic": {
            "CONFLICT": validated.coverage.get("DIAGNOSTIC", {}).get("CONFLICT", 0)
        },
        "gate": gate.model_dump(mode="json"),
    }
    if not privacy_safe_serialization(report, ()):
        raise ValueError("No-Answer report privacy validation failed")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "no_answer_threshold_report.v2.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if gate.outcome.value == "ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
