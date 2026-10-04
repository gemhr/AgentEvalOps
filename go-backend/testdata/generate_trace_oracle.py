"""离线读取真实 strict decoder/validator/digest，生成固定 trace-v1 golden."""

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.core.localagent.contract import TRACE_EXPORT_CONTRACT_FINGERPRINT
from app.core.localagent.decoder import decode_envelope_body, exact_json_dumps
from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.validation import (
    _canonical_json_object, _utc_iso, canonical_number, canonical_payload_digest,
    validate_contract, validate_envelope_semantics,
)

base = dict(contract_identity="localagent.runtime.trace_export", contract_version=1,
            contract_fingerprint=TRACE_EXPORT_CONTRACT_FINGERPRINT, run_id="runtime-1",
            trace_id="trace-1", span_id="span-1", parent_span_id=None, step_id=None,
            operation="runtime.run", component="agentcore", started_at="2026-10-04T00:00:00Z",
            completed_at="2026-10-04T00:00:01+00:00", duration_ms=1, status="OK", error_code=None,
            attributes={"runtime_mode": "direct", "step_count": 1})
vectors = []


def vector(name, change=None, raw=None):
    """使用真实 Python 合同产生预期值，不从 Go 生成 golden."""
    value = {**base, **(change or {})}
    raw = raw or exact_json_dumps(value)
    item = dict(id=name, raw=raw, accepted=False, algorithm_ref="trace-v1")
    try:
        envelope = LocalAgentTraceEnvelopeInV1.model_validate(decode_envelope_body(raw.encode("utf-8")))
        validate_contract(envelope)
        validate_envelope_semantics(envelope)
        payload = {field: getattr(envelope, field) for field in base}
        payload["contract_fingerprint"] = envelope.contract_fingerprint
        payload["started_at"] = _utc_iso(envelope.started_at)
        payload["completed_at"] = _utc_iso(envelope.completed_at)
        payload["duration_ms"] = canonical_number(envelope.duration_ms)
        canonical = _canonical_json_object(payload).encode("utf-8")
        digest = canonical_payload_digest(envelope)
        assert hashlib.sha256(canonical).hexdigest() == digest
        item.update(accepted=True, expected_canonical_bytes=base64.b64encode(canonical).decode(), expected_sha256=digest)
    except Exception as error:
        item["reject_reason"] = type(error).__name__
    vectors.append(item)


vector("base")
vector("duration_float_one", {"duration_ms": 1.0})
vector("duration_negative_zero", {"duration_ms": -0.0})
vector("duration_float_0_1", {"duration_ms": 0.1})
vector("duration_float_ten", {"duration_ms": 10.0})
vector("duration_subnormal", {"duration_ms": 5e-324})
vector("large_integer", {"duration_ms": 9007199254740993})
vector("max_duration_integer", {"duration_ms": 2**1024 - 2**970 - 1})
vector("huge_attribute", {"attributes": {"step_count": 10**4301 + 1}})
vector("attributes_order", {"attributes": {"step_count": 1, "runtime_mode": "direct"}})
vector("utc_microsecond", {"started_at": "2026-10-04T00:00:00.123456+00:00"})
vector("parent_step", {"operation": "runtime.step", "span_id": "step-span", "parent_span_id": "span-1", "step_id": "step-1", "attributes": {"dependency_count": 1, "result_char_count": 42}})
vector("duplicate_key", raw=exact_json_dumps(base).replace('"duration_ms":1', '"duration_ms":1,"duration_ms":2'))
vector("duplicate_attribute", raw=exact_json_dumps(base).replace('"step_count":1', '"step_count":1,"step_count":2'))
vector("invalid_schema", {"contract_version": 2})
vector("invalid_duration", {"duration_ms": -1})
vector("invalid_duration_bool", {"duration_ms": True})
vector("duration_over_limit", {"duration_ms": 2**1024})
vector("unknown_attribute", {"attributes": {"plan_content": "fake"}})
vector("unicode_identifier", {"component": "中文😀"})
vector("invalid_timezone", {"started_at": "2026-10-04T00:00:00+01:00"})
vector("error_missing_code", {"status": "ERROR"})
vector("invalid_float_attribute", {"attributes": {"step_count": 1.0}})
for name, value in [("unicode_codec", {"中文": "😀\u2028<>&\n"}),
                    ("float_codec", {"b": 0.1, "a": -0.0}),
                    ("integer_codec", {"n": 9007199254740993})]:
    canonical = _canonical_json_object(value).encode()
    vectors.append(dict(id=name, codec_only=True, raw=exact_json_dumps(value),
                        expected_canonical_bytes=base64.b64encode(canonical).decode(),
                        expected_sha256=hashlib.sha256(canonical).hexdigest()))
Path(__file__).with_name("trace_golden.json").write_text(json.dumps(vectors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"offline trace oracle: {len(vectors)} fixed vectors")

