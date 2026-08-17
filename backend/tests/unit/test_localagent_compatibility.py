"""Unit tests for the LocalAgent compatibility boundary (DTO / validation / digest / mapper)."""

# ruff: noqa: D415

import json
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.localagent.entities import (
    CONTRACT_FINGERPRINT_UNSUPPORTED,
    CONTRACT_IDENTITY_UNSUPPORTED,
    CONTRACT_VERSION_UNSUPPORTED,
    ENVELOPE_INVALID,
    LocalAgentContractFingerprintUnsupportedError,
    LocalAgentContractIdentityUnsupportedError,
    LocalAgentContractVersionUnsupportedError,
    LocalAgentEnvelopeInvalidError,
    LocalAgentTraceEnvelopeInV1,
    LocalAgentTraceEnvelopeOutV1,
)
from app.core.localagent.mapper import legacy_span_row, legacy_trace_row
from app.core.localagent.validation import (
    canonical_payload_digest,
    validate_contract,
    validate_envelope_semantics,
)
from app.core.localagent.contract import (
    CATEGORY_ATTRIBUTE_SCHEMAS,
    STABLE_OPERATIONS,
    TERMINAL_STATUSES,
    TRACE_EXPORT_CONTRACT_FINGERPRINT,
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
)
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"


def valid_payload(**overrides: object) -> dict[str, object]:
    """Return a fully valid frozen envelope payload (runtime.step, OK)."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-123",
        "trace_id": "trace-abc",
        "span_id": "span-xyz",
        "parent_span_id": None,
        "step_id": "step-1",
        "operation": "runtime.step",
        "component": "planner",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "duration_ms": 5000,
        "status": "OK",
        "error_code": None,
        "attributes": {
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "state": "SUCCEEDED",
            "result_char_count": 120,
        },
    }
    payload.update(overrides)
    return payload


def make_envelope(**overrides: object) -> LocalAgentTraceEnvelopeInV1:
    """Build a valid DTO from the frozen payload with optional overrides."""
    return LocalAgentTraceEnvelopeInV1.model_validate(valid_payload(**overrides))


# ---------------------------------------------------------------------------
# DTO exactness / extra=forbid
# ---------------------------------------------------------------------------


def test_request_dto_has_exact_frozen_fields():
    expected = {
        "contract_identity",
        "contract_version",
        "contract_fingerprint",
        "run_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "step_id",
        "operation",
        "component",
        "started_at",
        "completed_at",
        "duration_ms",
        "status",
        "error_code",
        "attributes",
    }
    assert set(LocalAgentTraceEnvelopeInV1.model_fields) == expected


def test_request_dto_rejects_legacy_fields():
    for legacy in ("input", "output", "metadata", "error", "session_id", "evaluation_run_id", "name", "kind"):
        with pytest.raises(ValidationError):
            make_envelope(**{legacy: "x"})


def test_request_dto_extra_forbid_unknown_top_level():
    with pytest.raises(ValidationError) as exc:
        make_envelope(unknown_field="boom")
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_response_dto_exact_fields():
    assert set(LocalAgentTraceEnvelopeOutV1.model_fields) == {"status", "error_code"}


def test_response_dto_extra_forbid():
    with pytest.raises(ValidationError):
        LocalAgentTraceEnvelopeOutV1(status="PERSISTED", error_code=None, extra="x")


def test_response_dto_rejects_invalid_status():
    with pytest.raises(ValidationError):
        LocalAgentTraceEnvelopeOutV1(status="ACCEPTED", error_code=None)


# ---------------------------------------------------------------------------
# Contract identity / version / fingerprint fail closed
# ---------------------------------------------------------------------------


def test_contract_identity_unsupported():
    with pytest.raises(LocalAgentContractIdentityUnsupportedError):
        validate_contract(make_envelope(contract_identity="other.contract"))


def test_contract_version_unsupported():
    with pytest.raises(LocalAgentContractVersionUnsupportedError):
        validate_contract(make_envelope(contract_version=2))


def test_contract_fingerprint_unsupported():
    with pytest.raises(LocalAgentContractFingerprintUnsupportedError):
        validate_contract(make_envelope(contract_fingerprint="a" * 64))


def test_contract_malformed_fingerprint_shape_rejected_at_dto():
    with pytest.raises(ValidationError):
        make_envelope(contract_fingerprint="not-a-digest")


def test_contract_valid_accepts():
    validate_contract(make_envelope())
    assert TRACE_EXPORT_CONTRACT_IDENTITY == "localagent.runtime.trace_export"
    assert TRACE_EXPORT_CONTRACT_VERSION == 1
    assert TRACE_EXPORT_CONTRACT_FINGERPRINT == FINGERPRINT


def test_contract_error_codes_match_frozen_vocabulary():
    assert LocalAgentContractIdentityUnsupportedError().error_code == CONTRACT_IDENTITY_UNSUPPORTED
    assert LocalAgentContractVersionUnsupportedError().error_code == CONTRACT_VERSION_UNSUPPORTED
    assert LocalAgentContractFingerprintUnsupportedError().error_code == CONTRACT_FINGERPRINT_UNSUPPORTED
    assert LocalAgentEnvelopeInvalidError().error_code == ENVELOPE_INVALID


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------

_DTO_SHAPE_CASES = [
    ("run_id", "bad id!"),
    ("trace_id", "x" * 129),
    ("span_id", "with space"),
    ("component", "bad/component"),
    ("parent_span_id", "bad id"),
    ("step_id", "bad id"),
    ("operation", "bad operation!"),
    ("status", "UNSET"),
    ("status", "RUNNING"),
]

# Non-finite durations are rejected at the strict Decimal DTO layer now.
_DTO_NON_FINITE_DURATION_CASES = [
    ("duration_ms", float("inf")),
    ("duration_ms", float("nan")),
]

_SEMANTIC_CASES = [
    ("operation", "not.an.operation"),
    ("started_at", "2026-01-01T00:00:00"),  # naive
    ("started_at", "2026-01-01T00:00:00+02:00"),  # non-UTC
    ("completed_at", "2026-01-01T00:00:00"),  # naive
]


def test_dto_rejects_invalid_shapes():
    for field, value in _DTO_SHAPE_CASES:
        with pytest.raises(ValidationError):
            make_envelope(**{field: value})


def test_dto_rejects_non_finite_duration():
    for field, value in _DTO_NON_FINITE_DURATION_CASES:
        with pytest.raises(ValidationError):
            make_envelope(**{field: value})


def test_dto_rejects_negative_duration():
    with pytest.raises(ValidationError):
        make_envelope(duration_ms=-1)


def test_semantic_validation_failures():
    for field, value in _SEMANTIC_CASES:
        with pytest.raises(LocalAgentEnvelopeInvalidError):
            validate_envelope_semantics(make_envelope(**{field: value}))


def test_completed_before_started_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(
            make_envelope(
                started_at="2026-01-01T00:00:05Z",
                completed_at="2026-01-01T00:00:00Z",
            )
        )


def test_step_correlation_missing_for_step_bound_operation():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(step_id=None))


def test_step_correlation_invalid_for_run_operation():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(
            make_envelope(
                operation="runtime.run",
                step_id="step-1",
                attributes={"planning_source": "deterministic", "final_status": "SUCCEEDED"},
            )
        )


def test_status_ok_requires_no_error_code():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(status="OK", error_code="boom"))


def test_status_non_ok_requires_error_code():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(status="ERROR", error_code=None))


def test_status_non_ok_valid_with_error_code():
    validate_envelope_semantics(make_envelope(status="ERROR", error_code="tool_error"))


def test_cancelled_and_timed_out_statuses_valid():
    validate_envelope_semantics(make_envelope(status="CANCELLED", error_code="cancelled"))
    validate_envelope_semantics(make_envelope(status="TIMED_OUT", error_code="timed_out"))


def test_unknown_attribute_key_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"not_exported": "x"}))


def test_internal_only_attribute_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"runtime_version": "1.0"}))


def test_attribute_none_value_rejected_at_dto():
    with pytest.raises(ValidationError):
        make_envelope(attributes={"execution_kind": None})


def test_attribute_type_invalid_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"dependency_count": "many"}))


def test_attribute_bool_rejected_for_int_domain():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"dependency_count": True}))


def test_attribute_domain_invalid_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"execution_kind": "NOT_A_KIND"}))


def test_attribute_range_domain_invalid_rejected():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(
            make_envelope(
                operation="runtime.output_delivery",
                step_id="step-1",
                attributes={"publish_attempt_count": 2},
            )
        )


def test_run_category_attributes_valid():
    validate_envelope_semantics(
        make_envelope(
            operation="runtime.run",
            step_id=None,
            attributes={
                "plan_id": "plan-1",
                "plan_version": 3,
                "plan_fingerprint": FINGERPRINT,
                "planning_source": "model_generated",
                "step_count": 4,
                "final_status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "shape": "1",
            },
        )
    )


def test_delivery_category_attributes_valid():
    validate_envelope_semantics(
        make_envelope(
            operation="runtime.output_delivery",
            step_id="step-9",
            attributes={
                "delivery_status": "DELIVERED",
                "gate_terminal_state": "PUBLISHED",
                "output_policy": "FINAL_PASSTHROUGH",
                "publish_attempt_count": 1,
                "partially_persisted": True,
                "output_char_count": 42,
            },
        )
    )


def test_memory_category_domain_validation():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(
            make_envelope(
                operation="runtime.final_memory_commit",
                step_id="step-9",
                attributes={"memory_scope": "session-wide"},
            )
        )


def test_contract_vocabulary_consistent():
    assert set(CATEGORY_ATTRIBUTE_SCHEMAS) == {"run", "planning", "step", "synthesis", "delivery", "memory"}
    assert {v[0] for v in STABLE_OPERATIONS.values()} == set(CATEGORY_ATTRIBUTE_SCHEMAS)
    assert TERMINAL_STATUSES == {"OK", "ERROR", "CANCELLED", "TIMED_OUT"}


# ---------------------------------------------------------------------------
# Canonical digest
# ---------------------------------------------------------------------------


def test_digest_stable_for_identical_payload():
    first = canonical_payload_digest(make_envelope())
    second = canonical_payload_digest(make_envelope())
    assert first == second
    assert len(first) == 64


def test_digest_independent_of_json_key_order_and_timezone_spelling():
    raw_a = json.dumps(valid_payload(), sort_keys=True)
    raw_b = json.dumps(valid_payload(), sort_keys=False)
    a = LocalAgentTraceEnvelopeInV1.model_validate_json(raw_a)
    b = LocalAgentTraceEnvelopeInV1.model_validate_json(raw_b)
    assert canonical_payload_digest(a) == canonical_payload_digest(b)

    tz_a = make_envelope(started_at="2026-01-01T00:00:00Z")
    tz_b = make_envelope(started_at="2026-01-01T00:00:00+00:00")
    assert canonical_payload_digest(tz_a) == canonical_payload_digest(tz_b)


def test_digest_changes_when_any_field_changes():
    base = canonical_payload_digest(make_envelope())
    assert canonical_payload_digest(make_envelope(span_id="span-other")) != base
    assert canonical_payload_digest(make_envelope(duration_ms=5001)) != base
    assert canonical_payload_digest(make_envelope(attributes={"execution_kind": "SYNTHESIS"})) != base
    assert canonical_payload_digest(make_envelope(component="other-comp")) != base
    assert canonical_payload_digest(make_envelope(status="ERROR", error_code="tool_error")) != base


def test_digest_excludes_server_local_fields_by_construction():
    payload = canonical_payload_digest(make_envelope())
    for forbidden in ("project_id", "envelope_id", "internal_trace_uuid", "created_at", "canonical_payload_digest"):
        assert forbidden not in payload


def test_int_and_float_duration_canonicalize_identically():
    a = make_envelope(duration_ms=5000)
    b = make_envelope(duration_ms=5000.0)
    assert canonical_payload_digest(a) == canonical_payload_digest(b)


# ---------------------------------------------------------------------------
# P1-01 strict wire typing (DTO layer, before any semantic check)
# ---------------------------------------------------------------------------


def test_contract_version_strict_wire_type():
    assert make_envelope().contract_version == 1
    for bad in ("1", 1.0, True, False):
        with pytest.raises(ValidationError):
            make_envelope(contract_version=bad)


def test_duration_ms_strict_wire_type():
    assert make_envelope(duration_ms=1).duration_ms == Decimal("1")
    assert make_envelope(duration_ms=1.5).duration_ms == Decimal("1.5")
    for bad in ("1.5", "1", True, False, None):
        with pytest.raises(ValidationError):
            make_envelope(duration_ms=bad)


def test_identifier_fields_strict_wire_type():
    """Identifiers/fingerprint/operation/component/status accept JSON strings only."""
    for field in (
        "contract_identity",
        "contract_fingerprint",
        "run_id",
        "trace_id",
        "span_id",
        "operation",
        "component",
        "status",
    ):
        with pytest.raises(ValidationError):
            make_envelope(**{field: 123})


def test_optional_identifier_fields_strict_wire_type():
    for field in ("parent_span_id", "step_id", "error_code"):
        with pytest.raises(ValidationError):
            make_envelope(**{field: 123})


def test_attributes_strict_scalars_at_dto():
    with pytest.raises(ValidationError):
        make_envelope(attributes={"execution_kind": ["AGENT"]})
    with pytest.raises(ValidationError):
        make_envelope(attributes={"execution_kind": {"x": 1}})
    with pytest.raises(ValidationError):
        make_envelope(attributes={"execution_kind": float("nan")})
    with pytest.raises(ValidationError):
        make_envelope(attributes={"execution_kind": None})


def test_attribute_per_key_wire_types_match_producer():
    """JSON boolean/int/string attribute types are preserved (no coercion)."""
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"dependency_count": "3"}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"dependency_count": 3.0}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"dependency_count": True}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"execution_kind": 123}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"partially_persisted": 0}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"partially_persisted": 1}))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        validate_envelope_semantics(make_envelope(attributes={"partially_persisted": "true"}))


# ---------------------------------------------------------------------------
# P1-03 exact semantic value domains (producer Owner vocabulary)
# ---------------------------------------------------------------------------


def test_planning_source_domain_exact():
    for value in ("deterministic", "legacy_adapter", "model_generated", "unknown"):
        validate_envelope_semantics(
            make_envelope(
                operation="runtime.run",
                step_id=None,
                attributes={"planning_source": value},
            )
        )
    for value in ("low", "medium", "high", "AGENT", "SYNTHESIS", "INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS"):
        with pytest.raises(LocalAgentEnvelopeInvalidError):
            validate_envelope_semantics(
                make_envelope(
                    operation="runtime.run",
                    step_id=None,
                    attributes={"planning_source": value},
                )
            )


def test_execution_kind_domain_exact():
    for value in ("AGENT", "SYNTHESIS"):
        validate_envelope_semantics(make_envelope(attributes={"execution_kind": value}))
    for value in ("INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS"):
        with pytest.raises(LocalAgentEnvelopeInvalidError):
            validate_envelope_semantics(make_envelope(attributes={"execution_kind": value}))


def test_delivery_status_domain_exact():
    for value in ("DELIVERED", "FAILED", "OUTCOME_UNKNOWN"):
        validate_envelope_semantics(
            make_envelope(
                operation="runtime.output_delivery",
                step_id="step-1",
                attributes={"delivery_status": value},
            )
        )
    for value in (
        "NOT_APPLICABLE",
        "OUTPUT_GATE_DUPLICATE_ATTEMPT",
        "OUTPUT_GATE_INTERNAL_STEP",
        "OUTPUT_GATE_NOT_FINAL",
        "OUTPUT_GATE_STEP_NOT_CLAIMED",
        "OUTPUT_GATE_STORE_NOT_READABLE",
    ):
        with pytest.raises(LocalAgentEnvelopeInvalidError):
            validate_envelope_semantics(
                make_envelope(
                    operation="runtime.output_delivery",
                    step_id="step-1",
                    attributes={"delivery_status": value},
                )
            )


# ---------------------------------------------------------------------------
# P1-04 numeric canonicalization / negative zero / digest
# ---------------------------------------------------------------------------


def test_duration_large_integers_never_collapse():
    a = canonical_payload_digest(make_envelope(duration_ms=9007199254740992))
    b = canonical_payload_digest(make_envelope(duration_ms=9007199254740993))
    assert a != b


def test_duration_zero_and_negative_zero_canonicalize_together():
    assert canonical_payload_digest(make_envelope(duration_ms=0)) == canonical_payload_digest(
        make_envelope(duration_ms=0.0)
    )
    # Python numeric equality: -0.0 == 0.0, so the producer semantics converge.
    assert canonical_payload_digest(make_envelope(duration_ms=0.0)) == canonical_payload_digest(
        make_envelope(duration_ms=-0.0)
    )


def test_canonical_number_unique_per_value():
    from app.core.localagent.validation import canonical_number

    assert canonical_number(Decimal("0")) == "0"
    assert canonical_number(Decimal("0.0")) == "0"
    assert canonical_number(Decimal("-0.0")) == "0"
    assert canonical_number(Decimal("5000")) == "5000"
    assert canonical_number(Decimal("5000.0")) == "5000"
    assert canonical_number(Decimal("1.50")) == "1.5"
    assert canonical_number(Decimal("1E+3")) == "1000"
    assert canonical_number(Decimal("9007199254740992")) == "9007199254740992"
    assert canonical_number(Decimal("9007199254740993")) == "9007199254740993"
    # 1.7976931348623157e308 normalizes to its 17 significant digits + zeros.
    assert canonical_number(Decimal("1.7976931348623157e308")) == "17976931348623157" + "0" * 292


def test_duration_outside_producer_domain_rejected():
    """10**400 (401 digits) exceeds MAX_V1_DURATION_INT -> rejected at DTO."""
    with pytest.raises(ValidationError):
        make_envelope(duration_ms=10**400)
    with pytest.raises(ValidationError):
        make_envelope(duration_ms=10**1000)


def test_duration_edge_matrix_accepted_rejected():
    for ok in (0, 0.0, -0.0, 1, 1.0, 9007199254740992, 9007199254740993, 123.456, 1.5e-9):
        envelope = make_envelope(duration_ms=ok)
        validate_envelope_semantics(envelope)
        assert canonical_payload_digest(envelope) == canonical_payload_digest(envelope)
    for bad in (float("nan"), float("inf"), float("-inf"), -1, 10**400):
        with pytest.raises((ValidationError, LocalAgentEnvelopeInvalidError)):
            validate_envelope_semantics(make_envelope(duration_ms=bad))


# ---------------------------------------------------------------------------
# Mapper (raw error absent / code-owned placeholders)
# ---------------------------------------------------------------------------


def test_legacy_mapper_keeps_raw_error_null_and_placeholders():
    envelope = make_envelope(status="ERROR", error_code="tool_error")
    trace_row = legacy_trace_row(envelope, internal_trace_uuid=uuid4(), project_id=uuid4())
    span_row = legacy_span_row(
        envelope,
        internal_span_uuid=uuid4(),
        internal_trace_uuid=trace_row["trace_id"],
        internal_parent_uuid=None,
    )
    assert span_row["error"] is None
    assert span_row["kind"] == SpanKind.OTHER.value
    assert span_row["status"] == SpanStatusCode.ERROR.value
    assert span_row["name"] == "localagent:runtime.step"
    assert trace_row["name"] == "localagent.trace"
    assert trace_row["status"] == TraceStatus.COMPLETED.value
    assert trace_row["input"] is None and trace_row["output"] is None
    assert span_row["input"] is None and span_row["output"] is None
    assert span_row["metadata"] == {}


def test_legacy_mapper_maps_cancelled_to_legacy_error_status():
    envelope = make_envelope(status="CANCELLED", error_code="cancelled")
    span_row = legacy_span_row(
        envelope,
        internal_span_uuid=uuid4(),
        internal_trace_uuid=uuid4(),
        internal_parent_uuid=None,
    )
    assert span_row["status"] == SpanStatusCode.ERROR.value


# ---------------------------------------------------------------------------
# Payload bound derivation
# ---------------------------------------------------------------------------


def test_worst_case_valid_envelope_fits_payload_bound():
    """The code-owned 16 KiB bound is derived: worst-case valid JSON is far smaller."""
    from app.api.v1.routes.localagent_integrations import LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES

    long_id = "a" * 128
    payload = valid_payload(
        run_id=long_id,
        trace_id=long_id,
        span_id=long_id,
        parent_span_id=long_id,
        step_id=long_id,
        component=long_id,
        operation="runtime.output_delivery",
        status="ERROR",
        error_code=long_id,
        attributes={
            "final_step_id": long_id,
            "output_policy": "FINAL_PASSTHROUGH",
            "delivery_status": "DELIVERED",
            "gate_terminal_state": "PUBLISHED",
            "publish_attempt_count": 1,
            "partially_persisted": True,
            "output_char_count": 2**31 - 1,
        },
    )
    raw = json.dumps(payload).encode("utf-8")
    assert len(raw) < 4096  # worst-case measured well under 4 KiB
    assert LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES >= 4 * len(raw)
    assert LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES <= 16 * 1024
