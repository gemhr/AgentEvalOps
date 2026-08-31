"""WP6-E isolated evaluation-environment provisioner wrapper + target/dataset certification。

复用 WP5 ``LocalAgentSubprocessProvisioner``（同一 subprocess / port / DB / env /
cleanup 体系，不建立第二套独立 subprocess system），为 Episodic 增加：

- ``certify_episodic_dataset``：strict loader 后重算 raw digest，必须匹配冻结 digest；
  否则 ``DATASET_MISMATCH``（experiment blocked，不得自动接受新 digest）。
- ``certify_episodic_target``：验证 LocalAgent target reachable、evaluation-execute/v3
  可用、target implementation ref 匹配冻结 ref；不匹配 -> BASELINE_INCOMPATIBLE /
  PREREQUISITE / BLOCKED。
- ``EpisodicEnvironmentProvisioner``：per-scenario fresh subprocess/port/Memory DB/
  Journal DB/environment token/workdir，并 build 绑定到 v3 endpoint 的
  ``EpisodicHttpEvaluationV3Target``。

Target implementation ref 由本模块按 target 冻结 manifest（mirror）重算，算法与
LocalAgent ``target_evaluation_implementation_ref`` 一致（relative + NUL + bytes + NUL）。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.adapters.evaluation.episodic_http_target import EpisodicHttpEvaluationV3Target
from app.core.evaluation.episodic_dataset import (
    EpisodicDataset,
    EpisodicScenario,
    content_digest,
)
from app.core.evaluation.execution import ExecutionTargetRef, VersionRef
from app.registry.settings import settings
from app.services.evaluation.stateful_environment import (
    HEALTH_PATH,
    LocalAgentSubprocessProvisioner,
    ScenarioEnvironmentEvidence,
    StatefulEnvironmentError,
)

#: 冻结的 Dataset raw digest（60 Gate；runner 启动时必须匹配）。
EPISODIC_FROZEN_DATASET_DIGEST: str = "sha256:d87ccfe28e414b90b8df10ffe3b1107b24f70dacf98e2996bb93f4950b105a2f"
#: G6-R1 scope-corrected V2 Dataset raw digest；V1 历史实验仍由上项认证。
EPISODIC_V2_FROZEN_DATASET_DIGEST: str = "sha256:678ecc706c6d0e199c057e7c45d94816679f9c9d9d6a9cc47b704e1b5f589e62"
#: 冻结的 LocalAgent target evaluation implementation ref（60 Gate）。
EPISODIC_FROZEN_TARGET_REF: str = "sha256:f7841bd8f1c178b7beaf9b64e8becc89bad64532d6c8a118a0e4bcada932492b"
#: LocalAgent 侧 frozen v3 target identity/version/config。
LOCALAGENT_V3_TARGET_ID = "localagent-coordinated-http"
LOCALAGENT_V3_TARGET_KIND = "LOCALAGENT_HTTP"
LOCALAGENT_V3_TARGET_VERSION = "evaluation-v3"
LOCALAGENT_V3_CONFIG = "localagent-episodic-evaluation-v1"

#: LocalAgent ``TARGET_EVALUATION_SEMANTIC_SOURCE_FILES`` 的显式 mirror（frozen manifest）。
TARGET_SEMANTIC_SOURCE_FILES: tuple[str, ...] = (
    "core/advanced_memory.py",
    "core/runtime/fault_injection_contract.py",
    "core/runtime/fault_injection.py",
    "core/runtime/multi_agent_driver.py",
    "core/runtime/multi_agent_planning.py",
    "core/runtime/plan_compiler.py",
    "core/runtime/episodic_evaluation.py",
    "core/runtime/run_coordinator.py",
    "core/runtime/runtime_factory.py",
    "core/chat_service.py",
    "core/agent_router.py",
    "core/runtime/episodic_memory_formation.py",
    "core/runtime/memory_retrieval.py",
    "core/runtime/model_context.py",
    "server.py",
    "tests/test_episodic_evaluation_harness.py",
    "core/settings.py",
    "core/llm_engine.py",
)


class EpisodicCertificationError(RuntimeError):
    """Dataset / target certification 失败（EVALUATION_INFRA / PREREQUISITE / BLOCKED）。"""


def compute_target_evaluation_implementation_ref(localagent_repo: Path) -> str:
    """按 target frozen manifest 重算 LocalAgent evaluation implementation ref。"""
    root = Path(localagent_repo)
    digest = hashlib.sha256()
    for relative in TARGET_SEMANTIC_SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            raise EpisodicCertificationError(f"target semantic source file missing: {path}")
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


def certify_episodic_dataset(
    dataset: EpisodicDataset,
    *,
    expected_digest: str | None = None,
) -> None:
    """校验已知 immutable Dataset lineage 的 digest；否则 DATASET_MISMATCH。"""
    if expected_digest is None:
        expected_digest = {
            ("stateful_episodic_v1", "v1"): EPISODIC_FROZEN_DATASET_DIGEST,
            ("stateful_episodic_v2", "v2"): EPISODIC_V2_FROZEN_DATASET_DIGEST,
        }.get((dataset.dataset_id, dataset.version))
    if expected_digest is None:
        raise EpisodicCertificationError(
            f"DATASET_MISMATCH: no frozen digest for {(dataset.dataset_id, dataset.version)!r}"
        )
    if dataset.content_digest != expected_digest:
        raise EpisodicCertificationError(f"DATASET_MISMATCH: got {dataset.content_digest}, expected {expected_digest}")


@dataclass(frozen=True, slots=True)
class EpisodicTargetCertification:
    """Target certification 结果。"""

    target_reachable: bool
    evaluation_execute_v3_available: bool
    actual_target_ref: str
    expected_target_ref: str
    ref_matches: bool

    @property
    def passed(self) -> bool:
        """Return the computed property value."""
        return self.target_reachable and self.evaluation_execute_v3_available and self.ref_matches


async def certify_episodic_target(
    base_url: str,
    localagent_repo: Path,
    *,
    expected_target_ref: str = EPISODIC_FROZEN_TARGET_REF,
) -> EpisodicTargetCertification:
    """验证 target reachable、v3 可用、target ref 匹配冻结 ref。"""
    reachable = False
    v3_available = False
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            health = await client.get(f"{base_url}{HEALTH_PATH}")
            reachable = health.status_code == 200
            if reachable:
                probe = await client.post(
                    f"{base_url}/api/runtime/evaluation-execute/v3",
                    json={
                        "agent_id": "core_router",
                        "query": "probe",
                        "run_id": "00000000-0000-0000-0000-000000000001",
                        "timeout_seconds": 0.001,
                    },
                )
                # 该 probe 会 422（invalid run_id policy 或 COORDINATED_REQUIRED）等；
                # 只要返回结构化 HTTP 响应且不是 404/405，即证明 v3 route 存在。
                v3_available = probe.status_code not in {404, 405}
    except httpx.HTTPError:
        reachable = False
    actual_ref = compute_target_evaluation_implementation_ref(localagent_repo)
    return EpisodicTargetCertification(
        target_reachable=reachable,
        evaluation_execute_v3_available=v3_available,
        actual_target_ref=actual_ref,
        expected_target_ref=expected_target_ref,
        ref_matches=actual_ref == expected_target_ref,
    )


def build_episodic_v3_target_ref() -> ExecutionTargetRef:
    """构造绑定 v3 target identity/version/config 的 ExecutionTargetRef。"""
    return ExecutionTargetRef(
        target_id=LOCALAGENT_V3_TARGET_ID,
        target_kind=LOCALAGENT_V3_TARGET_KIND,
        target_version_ref=VersionRef(
            kind="localagent_http_execution_target",
            opaque_value=LOCALAGENT_V3_TARGET_VERSION,
        ),
        config_ref=VersionRef(
            kind="localagent_http_config",
            opaque_value=LOCALAGENT_V3_CONFIG,
        ),
    )


class EpisodicEnvironmentProvisioner(Protocol):
    """为每个 Episodic Scenario 提供 fresh isolated 环境。"""

    async def provision(self, scenario: EpisodicScenario) -> ScenarioEnvironmentEvidence:
        """Create a fresh isolated environment for the scenario."""
        ...

    async def verify_bound(self, evidence: ScenarioEnvironmentEvidence) -> bool:
        """Verify the isolated environment binding is evidence-backed."""
        ...

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> EpisodicHttpEvaluationV3Target:
        """Build an executable target bound to the isolated environment."""
        ...

    async def cleanup(self, evidence: ScenarioEnvironmentEvidence, *, preserve: bool) -> None:
        """Release the environment; preserve=True keeps artifacts."""
        ...


class EpisodicLocalAgentProvisioner:
    """复用 LocalAgentSubprocessProvisioner 的 subprocess/DB/env/cleanup，构建 v3 target。"""

    def __init__(
        self,
        *,
        localagent_repo: Path,
        base_work_dir: Path,
        localagent_python_executable: str | Path | None = None,
        health_timeout_seconds: float = 60.0,
    ) -> None:
        self._inner = LocalAgentSubprocessProvisioner(
            localagent_repo=localagent_repo,
            base_work_dir=base_work_dir,
            localagent_python_executable=localagent_python_executable,
            health_timeout_seconds=health_timeout_seconds,
            subprocess_environment={"LOCAL_AGENT_RUNTIME_PROFILE": "EPISODIC_EVALUATION_LAYER1"},
        )

    async def provision(self, scenario: EpisodicScenario) -> ScenarioEnvironmentEvidence:
        """Create a fresh isolated environment for the scenario."""
        return await self._inner.provision(scenario)

    async def verify_bound(self, evidence: ScenarioEnvironmentEvidence) -> bool:
        """Verify the isolated environment binding is evidence-backed."""
        return await self._inner.verify_bound(evidence)

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> EpisodicHttpEvaluationV3Target:
        """Build an executable target bound to the isolated environment."""
        if evidence.localagent_base_url is None:
            raise StatefulEnvironmentError("subprocess evidence requires localagent_base_url")
        target_ref = build_episodic_v3_target_ref()
        return EpisodicHttpEvaluationV3Target(target_ref, evidence.localagent_base_url)

    async def cleanup(self, evidence: ScenarioEnvironmentEvidence, *, preserve: bool) -> None:
        """Release the environment; preserve=True keeps artifacts."""
        await self._inner.cleanup(evidence, preserve=preserve)


__all__ = [
    "EPISODIC_FROZEN_DATASET_DIGEST",
    "EPISODIC_V2_FROZEN_DATASET_DIGEST",
    "EPISODIC_FROZEN_TARGET_REF",
    "EpisodicCertificationError",
    "EpisodicEnvironmentProvisioner",
    "EpisodicLocalAgentProvisioner",
    "EpisodicTargetCertification",
    "LOCALAGENT_V3_CONFIG",
    "LOCALAGENT_V3_TARGET_ID",
    "LOCALAGENT_V3_TARGET_KIND",
    "LOCALAGENT_V3_TARGET_VERSION",
    "TARGET_SEMANTIC_SOURCE_FILES",
    "build_episodic_v3_target_ref",
    "certify_episodic_dataset",
    "certify_episodic_target",
    "compute_target_evaluation_implementation_ref",
]
