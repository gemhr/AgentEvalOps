"""Frozen LocalAgent Trace Export contract constants and semantic vocabulary.

Every literal in this module mirrors the frozen
``localagent.runtime.trace_export`` contract (contract version 1,
fingerprint ``6fc033bb...``) as consumed by the AgentEvalOps
compatibility endpoint.  The vocabulary is copied verbatim from the
LocalAgent WP4-A ``trace_export_contract`` module so that the server-side
consumer validation is exactly as strict as the producer contract — no
looser, no invented rules.

Values here are immutable contract truth.  Do NOT loosen them; a new
contract version must add a new validator/mapper instead.
"""

from __future__ import annotations

import re

# --- Contract identity -----------------------------------------------------

TRACE_EXPORT_CONTRACT_IDENTITY = "localagent.runtime.trace_export"
TRACE_EXPORT_CONTRACT_VERSION = 1
TRACE_EXPORT_CONTRACT_FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"

FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# --- Numeric duration contract (frozen by 65 / verified by 75) -----------------

# Largest integer the LocalAgent v1 shared validator accepts for duration_ms:
# the largest int whose conversion to binary64 stays finite and non-Infinity
# (``2**1024 - 2**970 - 1``, 309 decimal digits).  Integer tokens above this
# bound are outside the producer domain and must be rejected BEFORE any
# Decimal/string expansion.  ``sys.float_info.max`` is deliberately NOT used
# as the integer bound — that was the R1 semantic narrowing bug.
MAX_V1_DURATION_INT = 2**1024 - 2**970 - 1

# --- Identifier / operation shape rules --------------------------------------

# LocalAgent _ID: safe identifier (letters/digits/underscore/hyphen, 1..128).
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# LocalAgent _OPERATION: operation identifier allows dots (1..128).
OPERATION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# --- Terminal span status domain ---------------------------------------------

TERMINAL_STATUSES = frozenset({"OK", "ERROR", "CANCELLED", "TIMED_OUT"})

# --- Stable operations: operation -> (category, step_bound) -------------------

STABLE_OPERATIONS: dict[str, tuple[str, bool]] = {
    "runtime.run": ("run", False),
    "runtime.planning": ("planning", False),
    "runtime.step": ("step", True),
    "runtime.synthesis": ("synthesis", True),
    "runtime.output_delivery": ("delivery", True),
    "runtime.final_memory_commit": ("memory", True),
}

# --- Attribute value types -----------------------------------------------------

ATTR_BOOL = "BOOL"
ATTR_NON_NEGATIVE_INT = "NON_NEGATIVE_INT"
ATTR_FINITE_FLOAT = "FINITE_FLOAT"
ATTR_SAFE_IDENTIFIER = "SAFE_IDENTIFIER"
ATTR_DIGEST = "DIGEST"

# Attribute presence (only export-visible keys; INTERNAL_ONLY keys from the
# producer schema are intentionally absent and therefore rejected as unknown).
OPTIONAL = "OPTIONAL"
CONDITIONAL = "CONDITIONAL"

# --- Category attribute schemas: key -> (value_type, presence) ----------------

CATEGORY_ATTRIBUTE_SCHEMAS: dict[str, dict[str, tuple[str, str]]] = {
    "run": {
        "plan_id": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "plan_version": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "plan_fingerprint": (ATTR_DIGEST, OPTIONAL),
        "planning_source": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "step_count": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "selected_entry_agent_id": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "runtime_mode": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "final_status": (ATTR_SAFE_IDENTIFIER, CONDITIONAL),
        "stop_reason": (ATTR_SAFE_IDENTIFIER, CONDITIONAL),
        "shape": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
    },
    "planning": {
        "planning_source": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "schema_version": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "planner_model_invoked": (ATTR_BOOL, OPTIONAL),
        "compiled_shape": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "specialist_count": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "synthesis_required": (ATTR_BOOL, OPTIONAL),
    },
    "step": {
        "preferred_agent": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "execution_kind": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "output_policy": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "dependency_count": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "state": (ATTR_SAFE_IDENTIFIER, CONDITIONAL),
        "result_char_count": (ATTR_NON_NEGATIVE_INT, CONDITIONAL),
    },
    "synthesis": {
        "state": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "execution_kind": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
    },
    "delivery": {
        "final_step_id": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "output_policy": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "delivery_status": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "gate_terminal_state": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "publish_attempt_count": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
        "partially_persisted": (ATTR_BOOL, OPTIONAL),
        "output_char_count": (ATTR_NON_NEGATIVE_INT, OPTIONAL),
    },
    "memory": {
        "persist_enabled": (ATTR_BOOL, OPTIONAL),
        "entry_agent_id": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "memory_scope": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "delivery_status": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "user_write_status": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "assistant_write_status": (ATTR_SAFE_IDENTIFIER, OPTIONAL),
        "transaction_used": (ATTR_BOOL, OPTIONAL),
    },
}

# --- Field-level value domains (vocabulary / range) ----------------------------
# Vocabulary members are EXACT copies of the producer Owner value sets
# (``core/runtime/planning.py``: PlanSource/ExecutionKind/OutputPolicy,
# ``core/runtime/state.py``: RunStatus/StopReason,
# ``core/runtime/output_gate.py``: DeliveryStatus/OutputGateState,
# ``core/runtime/trace_export_contract.py``: _PLAN_SHAPE_VALUES, + "unknown").
# Consumer-only impossible values (e.g. ``low/medium/high``, ``INTERNAL``,
# ``FINAL_*``, ``OUTPUT_GATE_*``) are NOT members of any domain here.

_PLAN_SOURCE_VALUES = frozenset(
    {
        "deterministic",
        "legacy_adapter",
        "model_generated",
        "unknown",
    }
)
_PLAN_SHAPE_VALUES = frozenset({"0", "1", "2", "3", "unknown"})
_RUN_STATUS_VALUES = frozenset({"CREATED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"})
_STOP_REASON_VALUES = frozenset(
    {
        "COMPLETED",
        "UNHANDLED_ERROR",
        "DEADLINE_EXCEEDED",
        "USER_CANCELLED",
        "CLIENT_DISCONNECTED",
        "SYSTEM_SHUTDOWN",
        "MAX_STEPS_REACHED",
        "NO_ACTION",
        "REPEATED_ACTION",
        "BUDGET_EXHAUSTED",
        "PLANNING_FAILED",
    }
)
_EXECUTION_KIND_VALUES = frozenset({"AGENT", "SYNTHESIS"})
_OUTPUT_POLICY_VALUES = frozenset({"INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS"})
# Completed output_delivery spans never export NOT_APPLICABLE; the remaining
# DeliveryStatus members are exactly DELIVERED / FAILED / OUTCOME_UNKNOWN.
_DELIVERY_STATUS_EXPORT_VALUES = frozenset({"DELIVERED", "FAILED", "OUTCOME_UNKNOWN"})
# Completed delivery spans only write terminal gate states.
_GATE_TERMINAL_STATE_EXPORT_VALUES = frozenset({"PUBLISHED", "FAILED", "OUTCOME_UNKNOWN"})
_MEMORY_SCOPE_VALUES = frozenset({"direct"})
_MEMORY_WRITE_STATUS_VALUES = frozenset({"NOT_ATTEMPTED", "WRITTEN", "FAILED"})

# Domain descriptor: "vocabulary" -> frozenset; "range" -> (minimum, maximum).
CATEGORY_ATTRIBUTE_DOMAINS: dict[str, dict[str, tuple[str, frozenset | tuple[int, int]]]] = {
    "run": {
        "planning_source": ("vocabulary", _PLAN_SOURCE_VALUES),
        "final_status": ("vocabulary", _RUN_STATUS_VALUES),
        "stop_reason": ("vocabulary", _STOP_REASON_VALUES),
        "shape": ("vocabulary", _PLAN_SHAPE_VALUES),
    },
    "planning": {
        "planning_source": ("vocabulary", _PLAN_SOURCE_VALUES),
        "compiled_shape": ("vocabulary", _PLAN_SHAPE_VALUES),
    },
    "step": {
        "execution_kind": ("vocabulary", _EXECUTION_KIND_VALUES),
        "output_policy": ("vocabulary", _OUTPUT_POLICY_VALUES),
    },
    "synthesis": {
        "execution_kind": ("vocabulary", _EXECUTION_KIND_VALUES),
    },
    "delivery": {
        "output_policy": ("vocabulary", _OUTPUT_POLICY_VALUES),
        "delivery_status": ("vocabulary", _DELIVERY_STATUS_EXPORT_VALUES),
        "gate_terminal_state": ("vocabulary", _GATE_TERMINAL_STATE_EXPORT_VALUES),
        "publish_attempt_count": ("range", (0, 1)),
    },
    "memory": {
        "delivery_status": ("vocabulary", _DELIVERY_STATUS_EXPORT_VALUES),
        "memory_scope": ("vocabulary", _MEMORY_SCOPE_VALUES),
        "user_write_status": ("vocabulary", _MEMORY_WRITE_STATUS_VALUES),
        "assistant_write_status": ("vocabulary", _MEMORY_WRITE_STATUS_VALUES),
    },
}
