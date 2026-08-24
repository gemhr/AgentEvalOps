#!/usr/bin/env python
"""Run the WP5 evaluation-only Citation Context Selection comparison (no retrieval).

Consumes the frozen WP4 RRF evidence + a materialized controlled corpus + a
pinned tokenizer, runs ``fixed-top-k.v1`` for K=1..4, and writes strict
``citation-context-selection.v1`` sidecars plus a ``citation-context-comparison.v1``
report. It never re-runs retrieval or generation and never selects a winner.

``--help`` must not load the tokenizer/model, access the network, or touch
LocalAgent runtime.
"""

# ruff: noqa: D103, D415

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="frozen evaluation-dataset.v4 JSON")
    parser.add_argument("--rrf-evidence", type=Path, required=True, help="frozen no-answer-rrf-evidence.v2 JSON")
    parser.add_argument("--corpus", type=Path, required=True, help="materialized controlled-corpus asset JSON")
    parser.add_argument(
        "--source-manifest",
        type=Path,
        required=True,
        help="independent frozen source-manifest asset JSON (external source Authority)",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--tokenizer-path",
        type=Path,
        default=None,
        help="real generation-model GGUF path (llama-cpp); required for real WP5 token counts",
    )
    parser.add_argument(
        "--use-fixture-tokenizer",
        action="store_true",
        help="use the deterministic WP5 test-fixture tokenizer (TEST/STRUCTURAL only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # Heavy imports deferred until after --help exits.
    from app.core.evaluation.citation_context_selection import (
        K_VALUES,
        FixtureTokenCounter,
        privacy_safe_serialization,
    )
    from app.core.evaluation.dataset import load_dataset
    from app.core.evaluation.no_answer import RrfEvidenceEnvelopeV2
    from app.services.evaluation.citation_context_selection import (
        load_controlled_corpus,
        run_comparison,
        validate_frozen_inputs,
    )

    dataset = load_dataset(args.dataset)
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        json.loads(args.rrf_evidence.read_text(encoding="utf-8"))
    )
    corpus = load_controlled_corpus(args.corpus, args.source_manifest)
    validate_frozen_inputs(dataset, evidence, corpus)

    if args.use_fixture_tokenizer:
        if args.tokenizer_path is not None:
            raise ValueError("--use-fixture-tokenizer and --tokenizer-path are mutually exclusive")
        print("TOKENIZER_AUTHORITY = FIXTURE_TEST_ONLY (TEST/STRUCTURAL, not a real comparison)")
        counter = FixtureTokenCounter()
    else:
        if args.tokenizer_path is None:
            raise ValueError("--tokenizer-path is required for real WP5 token counts (or use --use-fixture-tokenizer)")
        from app.core.evaluation.citation_context_selection import LlamaCppTokenCounter

        # LlamaCppTokenCounter verifies the frozen ref + fresh GGUF SHA-256 and
        # applies the explicit frozen tokenization mode before lazy-loading.
        counter = LlamaCppTokenCounter(str(args.tokenizer_path))
        print("TOKENIZER_AUTHORITY = PINNED_GENERATION_MODEL (frozen GGUF + tokenization mode)")

    envelopes, report = run_comparison(
        dataset=dataset, evidence=evidence, corpus=corpus, token_counter=counter
    )
    if not privacy_safe_serialization(report.model_dump(mode="json")):
        raise ValueError("comparison report privacy validation failed")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for k in K_VALUES:
        envelope = envelopes[k]
        if not privacy_safe_serialization(envelope.model_dump(mode="json")):
            raise ValueError(f"K={k} sidecar privacy validation failed")
        (args.output_dir / f"citation-context-selection.k{k}.v1.json").write_text(
            json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    (args.output_dir / "citation-context-comparison.v1.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("WROTE comparison report:", args.output_dir / "citation-context-comparison.v1.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
