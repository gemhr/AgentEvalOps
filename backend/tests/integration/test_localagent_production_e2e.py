"""Opt-in cross-repository E2E against a real LocalAgent application.

The fixture starts ``uvicorn server:app`` with LocalAgent's real lifespan when
the required external PostgreSQL/Redis/JWT environment is supplied.  A
pre-started production-composed URL is also supported for CI/deployment
smoke runs.  The default is an explicit skip because this test must never
silently use a mock server or an unconfigured database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
)
from app.adapters.evaluation.http_localagent import LocalAgentHttpExecutionTarget
from app.core.evaluation import (
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionTargetRef,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.execution import OutcomeKind
from app.core.evaluation.run_attempts import AttemptStatus
from app.core.evaluation.results import EvaluationResultDraft, EvaluationVerdict
from app.core.evaluation.wp3_candidate_gate import WP3IdentityMismatch, validate_pair_identities
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation import (
    EvaluationComparisonService,
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluationPersistenceService,
    RegressionReportService,
    ResolvedEvaluator,
)
from app.services.evaluation.stateful_environment import LocalAgentSubprocessProvisioner
from app.registry.settings import settings

from .conftest import TEST_PROJECT_ID


@dataclass(frozen=True, slots=True)
class ProductionLocalAgent:
    """真实 LocalAgent URL、service credential 与可选 subprocess owner."""

    base_url: str
    bearer_token: str = field(repr=False)
    provisioner: LocalAgentSubprocessProvisioner | None = None
    evidence: object | None = None
    provider_state: _SequencedProviderState | None = None


class _SequencedProviderState:
    """同一 production profile 下按 evaluation run 产生可重复 good/good/bad 输出."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_index = 0
        self._request_count = 0
        self._all_requests_streaming = True

    def record_request(self, payload: Mapping[str, object]) -> None:
        """记录并约束当前 production adapter 的原生流式请求合同."""
        with self._lock:
            self._request_count += 1
            self._all_requests_streaming = (
                self._all_requests_streaming and payload.get("stream") is True
            )

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    @property
    def all_requests_streaming(self) -> bool:
        with self._lock:
            return self._all_requests_streaming

    def response_for(self, messages: list[dict[str, object]]) -> str:
        system = "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        )
        request = "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "user"
        )
        if "长期记忆候选提取器" in system:
            return '{"schema_version":1,"candidates":[]}'
        if "遗忘目标提取器" in system:
            return '{"schema_version":1,"logical_key":null,"source_excerpt":"","safe_reason":"EXPLICIT_FORGET"}'
        if "无需工具时仅输出" in system:
            return "NO_TOOL"
        if "LocalAgent Planner" in system:
            with self._lock:
                self._run_index += 1
            return (
                '{"schema_version":1,"decision":"DELEGATE","tasks":['
                '{"task_id":"answer","agent_id":"code_expert",'
                '"instruction":"deterministic evaluation task"}],'
                '"synthesis_required":true}'
            )
        del request
        with self._lock:
            run_index = self._run_index
        return "known-bad production completion" if run_index >= 3 else "Layer1 deterministic completion."


class _DeterministicProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI-compatible deterministic provider；不是 LocalAgent mock."""

    server: "_DeterministicProviderServer"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.state.record_request(payload)
        if payload.get("stream") is not True:
            self.send_error(422)
            return
        messages = payload.get("messages")
        if not isinstance(messages, list):
            self.send_error(422)
            return
        content = self.server.state.response_for(messages)
        delta = json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None,
                    }
                ]
            }
        )
        finish = json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        body = f"data: {delta}\n\ndata: {finish}\n\ndata: [DONE]\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        del args


class _DeterministicProviderServer(ThreadingHTTPServer):
    state: _SequencedProviderState


@pytest.fixture
def deterministic_provider() -> tuple[str, _SequencedProviderState]:
    """启动 bounded local model provider，供真实 LocalAgent remote adapter 调用."""
    server = _DeterministicProviderServer(("127.0.0.1", 0), _DeterministicProviderHandler)
    server.state = _SequencedProviderState()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_production_localagent_repr_redacts_bearer() -> None:
    value = ProductionLocalAgent("http://127.0.0.1:8000", "test-secret")
    assert "test-secret" not in repr(value)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"production LocalAgent E2E requires {name}")
    return value


@pytest.fixture
async def production_localagent(tmp_path: Path) -> ProductionLocalAgent:
    """提供真实 LocalAgent production lifespan；默认不启用外部 E2E."""
    if os.getenv("AGENTEVALOPS_RUN_LOCALAGENT_PRODUCTION_E2E", "") != "1":
        pytest.skip("set AGENTEVALOPS_RUN_LOCALAGENT_PRODUCTION_E2E=1 for production E2E")

    token = _required("LOCALAGENT_E2E_SERVICE_TOKEN")
    existing_url = os.getenv("LOCALAGENT_E2E_BASE_URL", "").strip()
    if existing_url:
        yield ProductionLocalAgent(existing_url.rstrip("/"), token)
        return

    localagent_repo = Path(
        os.getenv("LOCALAGENT_E2E_REPO", r"D:\PythonProject\Local_Agent")
    ).resolve()
    interpreter = Path(
        _required("LOCALAGENT_E2E_PYTHON_EXECUTABLE")
    ).expanduser().resolve()
    database_url = _required("LOCALAGENT_E2E_DATABASE_URL")
    redis_url = _required("LOCALAGENT_E2E_REDIS_URL")
    public_key = _required("LOCALAGENT_E2E_JWT_PUBLIC_KEY")
    scenario = SimpleNamespace(scenario_id="wp3-production-e2e")
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=localagent_repo,
        base_work_dir=tmp_path / "localagent",
        localagent_python_executable=interpreter,
        service_bearer_token=token,
        health_timeout_seconds=float(os.getenv("LOCALAGENT_E2E_HEALTH_TIMEOUT", "60")),
        health_poll_seconds=0.5,
        subprocess_environment={
            "LOCAL_AGENT_DATABASE_URL": database_url,
            "LOCAL_AGENT_REDIS_URL": redis_url,
            "LOCAL_AGENT_JWT_PUBLIC_KEY": public_key,
            "LOCAL_AGENT_RUNTIME_PROFILE": "EPISODIC_EVALUATION_LAYER1",
            "LOCAL_AGENT_KB_REQUIRED": "0",
            "LOCAL_AGENT_KAFKA_ENABLED": "false",
        },
    )
    evidence = await provisioner.provision(scenario)
    assert evidence.localagent_base_url is not None
    try:
        yield ProductionLocalAgent(evidence.localagent_base_url, token, provisioner, evidence)
    finally:
        await provisioner.cleanup(evidence, preserve=False)


@pytest.fixture
async def sequenced_production_localagent(
    tmp_path: Path,
    deterministic_provider: tuple[str, _SequencedProviderState],
    monkeypatch: pytest.MonkeyPatch,
) -> ProductionLocalAgent:
    """同一真实 remote-provider profile 下提供可比较的 good/good/bad runs."""
    if os.getenv("AGENTEVALOPS_RUN_LOCALAGENT_PRODUCTION_E2E", "") != "1":
        pytest.skip("set AGENTEVALOPS_RUN_LOCALAGENT_PRODUCTION_E2E=1 for production E2E")
    token = _required("LOCALAGENT_E2E_SERVICE_TOKEN")
    provider_url, provider_state = deterministic_provider
    monkeypatch.delenv("LOCAL_AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_EVALUATION_MODE", raising=False)
    localagent_repo = Path(os.getenv("LOCALAGENT_E2E_REPO", r"D:\PythonProject\Local_Agent")).resolve()
    interpreter = Path(_required("LOCALAGENT_E2E_PYTHON_EXECUTABLE")).expanduser().resolve()
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=localagent_repo,
        base_work_dir=tmp_path / "sequenced-localagent",
        localagent_python_executable=interpreter,
        service_bearer_token=token,
        health_timeout_seconds=float(os.getenv("LOCALAGENT_E2E_HEALTH_TIMEOUT", "60")),
        health_poll_seconds=0.5,
        subprocess_environment={
            "LOCAL_AGENT_DATABASE_URL": _required("LOCALAGENT_E2E_DATABASE_URL"),
            "LOCAL_AGENT_REDIS_URL": _required("LOCALAGENT_E2E_REDIS_URL"),
            "LOCAL_AGENT_JWT_PUBLIC_KEY": _required("LOCALAGENT_E2E_JWT_PUBLIC_KEY"),
            "LOCAL_AGENT_LLM_BACKEND": "remote",
            "LOCAL_AGENT_REMOTE_PROVIDER_KIND": "openai_compatible",
            "LOCAL_AGENT_REMOTE_API_BASE_URL": provider_url,
            "LOCAL_AGENT_REMOTE_MODEL_NAME": "wp3-deterministic-provider-v1",
            "LOCAL_AGENT_REMOTE_CONTEXT_WINDOW": "8192",
            "LOCAL_AGENT_MODEL_MAX_TOKENS": "512",
            "LOCAL_AGENT_REMOTE_ENABLE_THINKING": "0",
            "LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS": "10",
            "LOCAL_AGENT_REMOTE_VERIFY_TLS": "0",
            "LOCAL_AGENT_REMOTE_TRUST_ENV": "0",
            "LOCAL_AGENT_KB_REQUIRED": "0",
            "LOCAL_AGENT_KAFKA_ENABLED": "false",
        },
    )
    evidence = await provisioner.provision(SimpleNamespace(scenario_id="wp3-real-candidate-gate"))
    assert evidence.localagent_base_url is not None
    try:
        yield ProductionLocalAgent(
            evidence.localagent_base_url,
            token,
            provisioner,
            evidence,
            provider_state,
        )
    finally:
        await provisioner.cleanup(evidence, preserve=False)


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


def _target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        LOCALAGENT_HTTP_TARGET_ID,
        LOCALAGENT_HTTP_TARGET_KIND,
        LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
        config_ref=LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    )


class _ProductionTargetResolver:
    """EvaluationLoop resolver bound to the real fixture-owned HTTP target."""

    def __init__(self, target) -> None:
        self.target = target

    def resolve(self, target_ref: ExecutionTargetRef):
        assert target_ref == self.target.target_ref
        return self.target


class _PersistedPassEvaluator:
    """Deterministic evaluator that fails closed without final-answer evidence."""

    def __init__(self, spec: EvaluatorSpec) -> None:
        self.spec = spec

    async def evaluate(self, evaluation_input, context) -> EvaluationResultDraft:
        final_answer = tuple(
            ref for ref in evaluation_input.evidence_refs if ref.kind == "final_answer"
        )
        payload = final_answer[0].metadata.get("payload") if len(final_answer) == 1 else None
        actual = payload.get("content") if isinstance(payload, Mapping) else None
        expected = evaluation_input.expected_output
        passed = (
            isinstance(actual, str)
            and bool(actual.strip())
            and (expected is None or actual == expected)
        )
        return EvaluationResultDraft(
            self.spec.evaluator_id,
            self.spec.evaluator_version,
            self.spec.config_ref,
            EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
            "exact final-answer contract matched" if passed else "exact final-answer contract mismatched",
            score=1.0 if passed else 0.0,
            evidence_refs=final_answer,
            prompt_ref=self.spec.prompt_ref,
        )


class _ProductionEvaluatorResolver:
    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, _PersistedPassEvaluator(spec))


@pytest.mark.asyncio
async def test_authenticated_production_localagent_execution_persists_attempt(
    db_session, production_localagent: ProductionLocalAgent
) -> None:
    """真实 HTTP + production lifespan + AuthService 结果进入 PG Attempt."""
    case_ref = CaseVersionRef("wp3-e2e-case", "v1")
    now = datetime.now(UTC)
    case = CaseVersion(
        case_id=case_ref.case_id,
        version=case_ref.version,
        name="WP3 production E2E",
        input_payload={"agent_id": "core_router", "query": "return a deterministic answer"},
        created_at=now,
    )
    dataset = DatasetVersion(
        "wp3-e2e-dataset",
        "v1",
        "WP3 production E2E",
        now,
        case_version_refs=(case_ref,),
    )
    evaluator = EvaluatorSpec(
        "wp3-e2e-deterministic", "v1", EvaluatorKind.DETERMINISTIC,
        VersionRef("config", "wp3-e2e"), ScoreDirection.HIGHER_IS_BETTER,
    )
    suite = EvaluationSuiteVersion(
        "wp3-e2e-suite", "v1", (case_ref,), (evaluator,), EvaluationPolicy(), now,
    )
    persistence = _persistence()
    run, (attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=_target_ref(),
        timeout=timedelta(seconds=60),
    )
    claimed = await persistence.claim_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5), worker_ref="wp3-production-e2e"
    )
    assert claimed.claimed and claimed.claim_token is not None
    running = await persistence.start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token)

    if production_localagent.provisioner is not None:
        target = production_localagent.provisioner.build_target(production_localagent.evidence)
    else:
        from app.adapters.evaluation.http_localagent import LocalAgentHttpExecutionTarget

        target = LocalAgentHttpExecutionTarget(
            _target_ref(), production_localagent.base_url,
            bearer_token=production_localagent.bearer_token,
        )
    try:
        outcome = await target.execute(running.execution_request)
    finally:
        await target.aclose()
    terminal = await persistence.record_outcome(
        TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token, outcome
    )

    assert terminal.status is AttemptStatus.TERMINAL
    assert terminal.execution_outcome_kind is OutcomeKind.SUCCESS
    assert terminal.output_artifact_ref is not None
    assert terminal.output_artifact_ref.artifact_id == f"localagent-run://{attempt.attempt_id}"
    persisted = await persistence.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    assert persisted.execution_outcome_kind is OutcomeKind.SUCCESS
    assert persisted.run_id == run.run_id


@pytest.mark.asyncio
async def test_authenticated_production_baseline_candidate_pair_passes(db_session, production_localagent: ProductionLocalAgent) -> None:
    """两次真实 production execution 经完整 loop 持久化后可形成 PASS pair."""
    case_ref = CaseVersionRef("wp3-pair-case", "v1")
    now = datetime.now(UTC)
    case = CaseVersion(
        case_ref.case_id,
        case_ref.version,
        "WP3 production pair",
        {"agent_id": "core_router", "query": "return a deterministic answer"},
        now,
    )
    dataset = DatasetVersion(
        "wp3-pair-dataset", "v1", "WP3 production pair", now, case_version_refs=(case_ref,)
    )
    evaluator = EvaluatorSpec(
        "wp3-pair-deterministic",
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", "wp3-pair-deterministic-v1"),
        ScoreDirection.HIGHER_IS_BETTER,
        prompt_ref=VersionRef("prompt", "wp3-pair-deterministic-v1"),
    )
    suite = EvaluationSuiteVersion(
        "wp3-pair-suite", "v1", (case_ref,), (evaluator,), EvaluationPolicy(), now
    )
    persistence = _persistence()
    target_ref = _target_ref()
    baseline, (baseline_attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=60),
        metadata={"wp3_role": "BASELINE"},
    )
    candidate, (candidate_attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=60),
        metadata={"wp3_role": "CANDIDATE"},
    )

    if production_localagent.provisioner is not None:
        target = production_localagent.provisioner.build_target(production_localagent.evidence)
    else:
        target = LocalAgentHttpExecutionTarget(
            target_ref,
            production_localagent.base_url,
            bearer_token=production_localagent.bearer_token,
        )
    loop = EvaluationLoopService(
        persistence,
        _ProductionTargetResolver(target),
        _ProductionEvaluatorResolver(),
    )
    try:
        assert await loop.execute_attempt(
            TEST_PROJECT_ID, baseline_attempt.attempt_id, case, lease=timedelta(minutes=5)
        ) is EvaluationLoopResult.PROGRESSED
        assert await loop.execute_attempt(
            TEST_PROJECT_ID, candidate_attempt.attempt_id, case, lease=timedelta(minutes=5)
        ) is EvaluationLoopResult.PROGRESSED
    finally:
        await target.aclose()

    baseline_results = await persistence.list_results(TEST_PROJECT_ID, baseline.run_id)
    candidate_results = await persistence.list_results(TEST_PROJECT_ID, candidate.run_id)
    assert len(baseline_results) == len(candidate_results) == 1
    assert baseline_results[0].verdict is EvaluationVerdict.PASS
    assert candidate_results[0].verdict is EvaluationVerdict.PASS
    assert any(ref.kind == "final_answer" for ref in baseline_results[0].evidence_refs)
    assert any(ref.kind == "final_answer" for ref in candidate_results[0].evidence_refs)

    comparison = await EvaluationComparisonService(persistence).compare_runs(
        TEST_PROJECT_ID, baseline.run_id, candidate.run_id
    )
    report = RegressionReportService().build_report(comparison, (case_ref,))
    assert report.total_count == 1
    assert report.regression_count == 0
    assert report.release_decision.value == "PASS"


async def _execute_exact_answer_run(
    persistence: EvaluationPersistenceService,
    target,
    evaluator_resolver: _ProductionEvaluatorResolver,
    *,
    case: CaseVersion,
    dataset: DatasetVersion,
    suite: EvaluationSuiteVersion,
    role: str,
) -> tuple[object, object]:
    """通过真实 target 执行并持久化一个 exact-answer run."""
    case_ref = CaseVersionRef(case.case_id, case.version)
    run, (attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target.target_ref,
        timeout=timedelta(seconds=60),
        metadata={"wp3_role": role},
    )
    loop = EvaluationLoopService(
        persistence,
        _ProductionTargetResolver(target),
        evaluator_resolver,
    )
    result = await loop.execute_attempt(
        TEST_PROJECT_ID,
        attempt.attempt_id,
        case,
        lease=timedelta(minutes=5),
        worker_ref="wp3-real-candidate-gate",
    )
    assert result is EvaluationLoopResult.PROGRESSED
    persisted_attempt = await persistence.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    persisted_results = await persistence.list_results(TEST_PROJECT_ID, run.run_id)
    assert persisted_attempt.status is AttemptStatus.TERMINAL
    assert persisted_attempt.execution_outcome_kind is OutcomeKind.SUCCESS
    assert len(persisted_results) == 1
    return run, persisted_results[0]


def _run_canonical_gate(
    tmp_path: Path,
    *,
    label: str,
    baseline_run_id: object,
    candidate_run_id: object,
    case_ref: CaseVersionRef,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    """调用 canonical persisted mode；报告读取自本轮真实 run IDs."""
    configured_dir = os.getenv("AGENTEVALOPS_CANDIDATE_GATE_REPORT_DIR", "").strip()
    report_dir = Path(configured_dir) if configured_dir else tmp_path
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"release-gate-{label}.json"
    backend_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci.release_gate",
            "--project-id",
            str(TEST_PROJECT_ID),
            "--baseline-run-id",
            str(baseline_run_id),
            "--candidate-run-id",
            str(candidate_run_id),
            "--critical-case",
            f"{case_ref.case_id}@{case_ref.version}",
            "--report-json",
            str(report_path),
            "--dsn",
            settings.DATABASE_URL,
        ],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert report_path.is_file(), result.stderr
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["synthetic"] is False
    assert payload["authority"] == "RegressionReportService"
    return result, payload


@pytest.mark.asyncio
async def test_real_known_bad_candidate_fails_canonical_gate(
    db_session,
    sequenced_production_localagent: ProductionLocalAgent,
    tmp_path: Path,
) -> None:
    """真实 production HTTP good/bad runs 驱动 comparison/report 与 CLI exit."""
    case_ref = CaseVersionRef("wp3-real-known-bad", "v1")
    now = datetime.now(UTC)
    expected = "Layer1 deterministic completion."
    case = CaseVersion(
        case_ref.case_id,
        case_ref.version,
        "WP3 real known-bad candidate",
        {"agent_id": "core_router", "query": "return a deterministic answer"},
        now,
        expected_output=expected,
    )
    dataset = DatasetVersion(
        "wp3-real-known-bad-dataset",
        "v1",
        "WP3 real known-bad candidate",
        now,
        case_version_refs=(case_ref,),
    )
    evaluator = EvaluatorSpec(
        "wp3-pair-deterministic",
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", "wp3-pair-deterministic-v1"),
        ScoreDirection.HIGHER_IS_BETTER,
        prompt_ref=VersionRef("prompt", "wp3-pair-deterministic-v1"),
    )
    suite = EvaluationSuiteVersion(
        "wp3-real-known-bad-suite",
        "v1",
        (case_ref,),
        (evaluator,),
        EvaluationPolicy(),
        now,
    )
    persistence = _persistence()
    target = (
        sequenced_production_localagent.provisioner.build_target(sequenced_production_localagent.evidence)
        if sequenced_production_localagent.provisioner is not None
        else LocalAgentHttpExecutionTarget(
            _target_ref(),
            sequenced_production_localagent.base_url,
            bearer_token=sequenced_production_localagent.bearer_token,
        )
    )
    resolver = _ProductionEvaluatorResolver()
    try:
        baseline, baseline_result = await _execute_exact_answer_run(
            persistence,
            target,
            resolver,
            case=case,
            dataset=dataset,
            suite=suite,
            role="BASELINE",
        )
        good, good_result = await _execute_exact_answer_run(
            persistence,
            target,
            resolver,
            case=case,
            dataset=dataset,
            suite=suite,
            role="CANDIDATE",
        )
        bad, bad_result = await _execute_exact_answer_run(
            persistence,
            target,
            resolver,
            case=case,
            dataset=dataset,
            suite=suite,
            role="CANDIDATE",
        )
    finally:
        await target.aclose()

    assert baseline_result.verdict is EvaluationVerdict.PASS
    assert good_result.verdict is EvaluationVerdict.PASS
    assert bad_result.verdict is EvaluationVerdict.FAIL
    assert baseline_result.score == good_result.score == 1.0
    assert bad_result.score == 0.0
    assert baseline.dataset_snapshot == good.dataset_snapshot == bad.dataset_snapshot
    assert baseline.suite_snapshot == good.suite_snapshot == bad.suite_snapshot
    assert baseline.execution_target_ref == good.execution_target_ref == bad.execution_target_ref
    assert baseline_result.config_ref == good_result.config_ref == bad_result.config_ref
    assert baseline_result.prompt_ref == good_result.prompt_ref == bad_result.prompt_ref
    comparison = await EvaluationComparisonService(persistence).compare_runs(
        TEST_PROJECT_ID, baseline.run_id, bad.run_id
    )
    report = RegressionReportService().build_report(comparison, (case_ref,))
    assert report.regression_count == 1
    assert report.release_decision.value == "FAIL"

    run_ids_path = os.getenv("AGENTEVALOPS_CANDIDATE_GATE_RUN_IDS_PATH", "").strip()
    if run_ids_path:
        target = Path(run_ids_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "project_id": str(TEST_PROJECT_ID),
                    "baseline_run_id": str(baseline.run_id),
                    "good_candidate_run_id": str(good.run_id),
                    "bad_candidate_run_id": str(bad.run_id),
                    "synthetic": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    good_gate, good_payload = _run_canonical_gate(
        tmp_path,
        label="good",
        baseline_run_id=baseline.run_id,
        candidate_run_id=good.run_id,
        case_ref=case_ref,
    )
    bad_gate, bad_payload = _run_canonical_gate(
        tmp_path,
        label="known-bad",
        baseline_run_id=baseline.run_id,
        candidate_run_id=bad.run_id,
        case_ref=case_ref,
    )
    assert good_gate.returncode == 0, good_gate.stderr
    assert bad_gate.returncode == 2, bad_gate.stderr
    assert good_payload["release_decision"] == "PASS"
    assert bad_payload["release_decision"] == "FAIL"
    assert bad_payload["comparison_counts"]["regressions"] == 1
    assert sequenced_production_localagent.provider_state is not None
    assert sequenced_production_localagent.provider_state.request_count > 0
    assert sequenced_production_localagent.provider_state.all_requests_streaming


def test_validate_pair_identities_rejects_source_manifest_mismatch() -> None:
    """source-index provenance mismatch must fail closed before comparison."""
    from tests.unit.test_wp3_candidate_gate import _identity

    baseline = _identity("BASELINE")
    candidate = replace(_identity("CANDIDATE", "HYBRID_RRF"), source_manifest_sha256="x" * 64)
    with pytest.raises(WP3IdentityMismatch, match="source_manifest_sha256"):
        validate_pair_identities(baseline, candidate)
