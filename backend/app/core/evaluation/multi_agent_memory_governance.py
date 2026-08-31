"""WP7-E V2 dataset contract, typed v4 observations, and governance evaluators."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator


SCHEMA_VERSION = "multi-agent-memory-governance.v2"
DATASET_ID = "multi_agent_memory_governance_v2"
DATASET_VERSION = "v2"


class DatasetLoadError(ValueError):
    """Dataset bytes cannot be safely decoded or parsed."""


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: StrictStr
    agent_id: StrictStr
    permissions: list[Literal["READ", "WRITE", "PROMOTE", "FORGET"]]


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "PRIVATE_READ",
        "PRIVATE_FORGET",
        "PROJECT_READ",
        "PROJECT_WRITE",
        "PROJECT_FORGET",
        "PRIVATE_TO_PROJECT_PROMOTION",
    ]
    target_owner_agent_id: StrictStr | None = None
    logical_key: StrictStr | None = None
    canonical_text: StrictStr | None = None
    source_memory_id: StrictStr | None = None


class PrivateFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture_ref: StrictStr
    owner_agent_id: StrictStr
    logical_key: StrictStr
    canonical_text: StrictStr


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: Literal["run_a", "run_b", "run_c"]
    requester_agent_id: StrictStr
    project_id: StrictStr | None = None
    grants: list[Grant] = Field(default_factory=list)
    private_fixtures: list[PrivateFixture] = Field(default_factory=list)
    operation: Operation | None = None
    deterministic_multi_agent: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> "Run":
        if self.grants and not self.project_id:
            raise ValueError("grants require project_id")
        if self.project_id and any(grant.project_id != self.project_id for grant in self.grants):
            raise ValueError("grant project identity mismatch")
        if self.operation and self.operation.operation.startswith("PROJECT") and not self.project_id:
            raise ValueError("project operation requires project_id")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: StrictStr
    runs: list[Run]
    required_surface: Literal[
        "private_authorization",
        "project_retrieval",
        "project_mutation",
        "promotion",
        "specialist",
        "delegation",
        "context_trust",
    ]

    @model_validator(mode="after")
    def _ordering(self) -> "Scenario":
        ids = [run.run_id for run in self.runs]
        if ids != ["run_a"] and ids != ["run_a", "run_b"] and ids != ["run_a", "run_b", "run_c"]:
            raise ValueError("runs must be ordered run_a, run_b, run_c without gaps")
        if self.scenario_id in {"G04", "G05", "G06", "G07", "G12"} and len(self.runs) < 2:
            raise ValueError(f"{self.scenario_id} requires prior durable state")
        return self


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_schema_version: Literal[SCHEMA_VERSION]
    dataset_id: Literal[DATASET_ID]
    version: Literal[DATASET_VERSION]
    parent_dataset_id: Literal["multi_agent_memory_governance_v1"]
    parent_version: Literal["v1"]
    parent_digest: StrictStr
    remediation_reason: Literal["STATEFUL_EVIDENCE_DEFECT"]
    execution_policy: Literal["GLOBAL_SEQUENTIAL"]
    scenarios: list[Scenario]

    @model_validator(mode="after")
    def _inventory(self) -> "Dataset":
        if [item.scenario_id for item in self.scenarios] != [f"G{i:02d}" for i in range(1, 13)]:
            raise ValueError("scenario inventory must be ordered G01..G12")
        return self


def load_dataset(path: str | Path) -> Dataset:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise DatasetLoadError(f"cannot read governance dataset: {path}") from exc
    except UnicodeDecodeError as exc:
        raise DatasetLoadError("governance dataset is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise DatasetLoadError("governance dataset is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DatasetLoadError("governance dataset must be a JSON object")
    return Dataset.model_validate(value)


class AuthorizationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["ALLOW", "DENY"]
    reason: StrictStr | None = None
    operation: StrictStr | None = None
    requester: StrictStr | None = None
    owner_safe_identity: StrictStr | None = None
    project_safe_identity: StrictStr | None = None
    visibility: StrictStr | None = None
    owner_match: bool | None = None
    scope_match: bool | None = None
    grant_match: bool | None = None
    affected_count: StrictInt | None = None
    owner_kind: StrictStr | None = None
    grant_type: StrictStr | None = None


class RetrievalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_count: StrictInt
    selected_count: StrictInt
    supplied_count: StrictInt
    injected_count: StrictInt
    safe_memory_refs: list[StrictStr] = Field(default_factory=list)
    context_sources: list[dict[str, object]] = Field(default_factory=list)


class MutationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_count: StrictInt
    affected_count: StrictInt
    after_count: StrictInt
    outcome: StrictStr
    operation: StrictStr | None = None
    safe_target_ref: StrictStr | None = None


class PromotionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["ALLOW", "DENY"]
    provenance_complete: bool
    resulting_project_memory_ref: StrictStr | None = None
    source_private_memory_ref: StrictStr | None = None
    source_owner_safe_identity: StrictStr | None = None
    promoter_safe_identity: StrictStr | None = None
    target_project_safe_identity: StrictStr | None = None
    outcome: StrictStr | None = None


class SpecialistObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified_performer: StrictStr
    episode_owner: StrictStr
    run_id: StrictStr | None = None
    step_id: StrictStr | None = None
    planned_agent: StrictStr | None = None
    binding_agent: StrictStr | None = None
    claim_agent: StrictStr | None = None
    producer_agent: StrictStr | None = None
    episode_kind: StrictStr | None = None
    formation_outcome: StrictStr | None = None
    idempotency_outcome: StrictStr | None = None


class ContextTrustObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: StrictStr
    trust_role: StrictStr


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization: AuthorizationObservation | None
    private_retrieval: RetrievalObservation
    project_retrieval: RetrievalObservation
    mutation: MutationObservation | None
    promotion: PromotionObservation | None
    specialist_formation: list[SpecialistObservation]
    invocation_visibility: list[dict[str, object]]

    @classmethod
    def from_response(cls, body: object) -> "Observation":
        if not isinstance(body, dict):
            raise ValueError("v4 response must be object")
        return cls.model_validate(
            {
                "authorization": body.get("authorization"),
                "private_retrieval": body.get("private_retrieval"),
                "project_retrieval": body.get("project_retrieval"),
                "mutation": body.get("mutation"),
                "promotion": body.get("promotion"),
                "specialist_formation": body.get("specialist_formation", []),
                "invocation_visibility": body.get("invocation_visibility", []),
            }
        )


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


def _zero(item: RetrievalObservation) -> bool:
    return not any((item.candidate_count, item.selected_count, item.supplied_count, item.injected_count))


class PrivateAuthorizationEvaluator:
    def evaluate(self, scenario_id: str, observations: list[Observation]) -> Verdict:
        item = observations[-1]
        if item.authorization is None:
            return Verdict.BLOCKED
        if scenario_id == "G01":
            return (
                Verdict.PASS
                if item.authorization.decision == "ALLOW"
                and item.private_retrieval.selected_count > 0
                and item.private_retrieval.injected_count > 0
                else Verdict.FAIL
            )
        if scenario_id == "G02":
            return (
                Verdict.PASS
                if item.authorization.decision == "DENY" and _zero(item.private_retrieval)
                else Verdict.FAIL
            )
        if scenario_id == "G03":
            return (
                Verdict.PASS
                if item.authorization.decision == "DENY"
                and item.mutation
                and item.mutation.before_count > 0
                and item.mutation.affected_count == 0
                else Verdict.FAIL
            )
        return Verdict.BLOCKED


class ProjectRetrievalEvaluator:
    def evaluate(self, scenario_id: str, observations: list[Observation]) -> Verdict:
        prior, item = observations[0], observations[-1]
        if prior.mutation is None or prior.mutation.affected_count <= 0:
            return Verdict.BLOCKED
        r = item.project_retrieval
        if scenario_id == "G04":
            return (
                Verdict.PASS
                if item.authorization
                and item.authorization.decision == "ALLOW"
                and all(x > 0 for x in (r.candidate_count, r.selected_count, r.supplied_count, r.injected_count))
                else Verdict.FAIL
            )
        if scenario_id == "G05":
            return Verdict.PASS if _zero(r) else Verdict.FAIL
        if scenario_id == "G06":
            return (
                Verdict.PASS
                if item.authorization and item.authorization.decision == "DENY" and _zero(r)
                else Verdict.FAIL
            )
        return Verdict.BLOCKED


class ProjectMutationEvaluator:
    def evaluate(self, observations: list[Observation]) -> Verdict:
        item = observations[-1]
        return (
            Verdict.PASS
            if item.authorization
            and item.authorization.decision == "DENY"
            and item.mutation
            and item.mutation.affected_count == 0
            else Verdict.FAIL
        )


class PromotionEvaluator:
    def evaluate(self, scenario_id: str, observations: list[Observation]) -> Verdict:
        item = observations[-1]
        if not item.promotion:
            return Verdict.BLOCKED
        if scenario_id == "G08":
            return (
                Verdict.PASS
                if item.promotion.decision == "ALLOW" and item.promotion.provenance_complete
                else Verdict.FAIL
            )
        return (
            Verdict.PASS
            if item.promotion.decision == "DENY" and not item.promotion.resulting_project_memory_ref
            else Verdict.FAIL
        )


class SpecialistOwnershipEvaluator:
    def evaluate(self, item: Observation) -> Verdict:
        return (
            Verdict.PASS
            if item.specialist_formation
            and all(x.verified_performer == x.episode_owner for x in item.specialist_formation)
            else Verdict.FAIL
        )


class DelegationBoundaryEvaluator:
    def evaluate(self, item: Observation) -> Verdict:
        return (
            Verdict.PASS
            if item.invocation_visibility
            and all(not bool(x.get("private_bundle_present")) for x in item.invocation_visibility)
            else Verdict.FAIL
        )


class ContextTrustEvaluator:
    def evaluate(self, observations: list[Observation]) -> Verdict:
        prior, item = observations[0], observations[-1]
        sources = [ContextTrustObservation.model_validate(x) for x in item.project_retrieval.context_sources]
        if prior.mutation is None or prior.mutation.affected_count <= 0:
            return Verdict.BLOCKED
        return (
            Verdict.PASS
            if item.project_retrieval.injected_count > 0
            and any(x.source_type == "project_memory_retrieval" and x.trust_role == "user_content" for x in sources)
            else Verdict.FAIL
        )


def evaluate_scenario(scenario_id: str, observations: list[Observation]) -> Verdict:
    if scenario_id in {"G01", "G02", "G03"}:
        return PrivateAuthorizationEvaluator().evaluate(scenario_id, observations)
    if scenario_id in {"G04", "G05", "G06"}:
        return ProjectRetrievalEvaluator().evaluate(scenario_id, observations)
    if scenario_id == "G07":
        return ProjectMutationEvaluator().evaluate(observations)
    if scenario_id in {"G08", "G09"}:
        return PromotionEvaluator().evaluate(scenario_id, observations)
    if scenario_id == "G10":
        return SpecialistOwnershipEvaluator().evaluate(observations[-1])
    if scenario_id == "G11":
        return DelegationBoundaryEvaluator().evaluate(observations[-1])
    if scenario_id == "G12":
        return ContextTrustEvaluator().evaluate(observations)
    raise ValueError(f"unknown scenario {scenario_id}")


__all__ = [
    "DATASET_ID",
    "DATASET_VERSION",
    "Dataset",
    "DatasetLoadError",
    "Observation",
    "Verdict",
    "evaluate_scenario",
    "load_dataset",
]
