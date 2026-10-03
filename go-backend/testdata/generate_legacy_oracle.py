"""仅离线生成固定 legacy golden corpus；Go tests 不调用 Python。"""

import base64
from dataclasses import fields, is_dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.evaluation.catalog import (  # noqa: E402
    AssertionSpec, EvaluatorKind, EvaluatorSpec, ScoreDirection, TestCaseVersion,
)
from app.core.evaluation.immutable import json_compatible  # noqa: E402
from app.core.evaluation.references import ArtifactRef, EvidenceRef, VersionRef  # noqa: E402


def plain(value):
    """展开 frozen containers，并保留 Python int/float。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return json_compatible(value)


def vector(name, kind, snapshot, fields, expected):
    """字节来自同一冻结投影，并核对当前真实 domain 的 property。"""
    projection = {key: snapshot[key] for key in fields}
    raw = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    assert expected == digest
    return {"vector_id": name, "kind": kind, "algorithm_ref": "legacy-python-json-v1",
            "snapshot_json": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            "expected_canonical_bytes_base64": base64.b64encode(raw).decode(), "expected_sha256": digest}


def main():
    """生成后 expected bytes/hash 固定提交；不是运行时 fallback。"""
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    corpus = []
    fields = ["input_payload", "expected_output", "assertion_specs", "fixture_refs", "evidence_refs", "metadata"]
    values = {
        "unicode_escape": {"中": "😀\u2028<>&\"\\\n\t\b\f\r\x00", "a": {"z": 1, "a": None}},
        "ordered_array": [3, 1, 2, None, True, False, {"b": 2, "a": 1}],
        "int": 1, "float": 1.0, "negative_zero": -0.0,
        "numbers": [1e-7, 1e20, 1e-4, 1e-5, 1e15, 1e16, 0.1, 5e-324, 1.7976931348623157e308],
        "large_integers": [9007199254740992, 9007199254740993, 10**100, -10**100],
        "null": None,
    }
    for name, value in values.items():
        case = TestCaseVersion("legacy-case", "v1", "name", value, now,
            assertion_specs=(AssertionSpec("a", "contract", {"k": 1.0}), AssertionSpec("b", "contract", {"k": None}, False)),
            fixture_refs=(ArtifactRef("fixture:1", digest="opaque", metadata={"unicode": "中"}),),
            evidence_refs=(EvidenceRef("plan", "evidence:1", schema_version="v1"),), metadata={"nested": {"z": 1, "a": 2}})
        snapshot = plain(case)
        corpus.append(vector(name, "CASE", snapshot, fields, case.semantic_digest))
    spec_fields = ["evaluator_id", "evaluator_version", "evaluator_kind", "config_ref", "config_snapshot", "threshold", "score_direction", "score_range", "comparison_tolerance", "prompt_ref", "required", "result_schema_ref", "comparison_semantics", "required_artifact_kinds", "required_evidence_kinds"]
    for required in (True, False):
        spec = EvaluatorSpec("legacy-judge", "v1", EvaluatorKind.LLM_JUDGE, VersionRef("config", "c1"),
            ScoreDirection.HIGHER_IS_BETTER, config_snapshot={"n": 1.0, "min": -0.0, "中": "<>&😀"},
            threshold=0.5, score_range=(0.0, 1.0), comparison_tolerance=1e-7,
            prompt_ref=VersionRef("prompt", "p1"), required=required,
            required_artifact_kinds=("answer",), required_evidence_kinds=("context",))
        snapshot = plain(spec)
        corpus.append(vector(f"spec-required-{required}", "SPEC", snapshot, spec_fields, spec.definition_digest))
    Path(__file__).with_name("legacy_catalog_golden.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
