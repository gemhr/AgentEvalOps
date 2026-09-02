"""WP5 Stateful Memory 的 evaluation-environment provisioner。

每个 Scenario 在执行前获得 fresh isolated LocalAgent Memory DB + journal DB，绑定
专属 LocalAgent instance/configuration，并以证据验证绑定（不能只设置一个变量后假设
成功）。本模块只负责环境供给与绑定验证；不做任何 Memory 决策。

- ``LocalAgentSubprocessProvisioner`` 以 per-process env vars
  （``LOCAL_AGENT_MEMORY_DB_PATH`` / ``LOCAL_AGENT_EVENT_JOURNAL_DB_PATH`` /
  ``LOCAL_AGENT_API_PORT`` / ``LOCAL_AGENT_ENVIRONMENT_ID``）启动私有 LocalAgent
  实例；DB/journal 路径完全由 AgentEvalOps 拥有（isolated evaluation environment）。
- LocalAgent runtime interpreter 必须显式配置（constructor 参数优先，
  其次 AgentEvalOps Settings 的 ``LOCALAGENT_PYTHON_EXECUTABLE``）；缺失/不存在时
  必须 fail closed（``EVALUATION_INFRA_FAILURE``），绝不静默 fallback 到
  ``sys.executable``。AgentEvalOps interpreter 与 LocalAgent interpreter 可以不同。
- fixture seed 只允许 Layer 1 deterministic harness（initial_state=SEEDED），且必须
  在 scenario DB 创建后、任何 target invocation 前写入。
- 任何 provision/identity/cleanup 失败都是 ``EVALUATION_INFRA_FAILURE``，不计为
  LocalAgent/model 质量失败。不打印/持久化任何 provider secret。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

import httpx

from app.core.evaluation.execution import ExecutionTarget
from app.core.evaluation.stateful_memory_dataset import (
    InitialMemoryStateKind,
    StatefulMemoryScenario,
)
from app.core.evaluation.stateful_projection import FORGET_TOMBSTONE_TEXT
from app.registry.settings import settings

SCENARIO_TOKEN_FILE = "scenario_token.json"
SCENARIO_MEMORY_DB_FILE = "memory.db"
SCENARIO_JOURNAL_DB_FILE = "event_journal.db"
HEALTH_PATH = "/health"
LOCALAGENT_MEMORY_DB_ENV = "LOCAL_AGENT_MEMORY_DB_PATH"
LOCALAGENT_JOURNAL_DB_ENV = "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH"
LOCALAGENT_PORT_ENV = "LOCAL_AGENT_API_PORT"
LOCALAGENT_HOST_ENV = "LOCAL_AGENT_API_HOST"
LOCALAGENT_ENVIRONMENT_ID_ENV = "LOCAL_AGENT_ENVIRONMENT_ID"
LOCALAGENT_ENVIRONMENT_PROFILE_ENV = "LOCAL_AGENT_ENVIRONMENT_PROFILE"
# AgentEvalOps-owned setting selecting the LocalAgent runtime interpreter。
# 这不是 LocalAgent 自己的 production setting（LocalAgent 使用 LOCAL_AGENT_*）。
LOCALAGENT_PYTHON_EXECUTABLE_SETTING = "LOCALAGENT_PYTHON_EXECUTABLE"
STARTUP_DIAGNOSTIC_FILE = "startup_diagnostic.json"
STARTUP_STDOUT_FILE = "startup.stdout.log"
STARTUP_STDERR_FILE = "startup.stderr.log"
MAX_STARTUP_DIAGNOSTIC_BYTES = 4096


@dataclass(frozen=True, slots=True)
class StartupDiagnostic:
    """Provisioning startup failure 的 private、bounded、sanitized evidence。"""

    process_exit_code: int | None
    startup_failure_phase: str
    interpreter_path: str
    working_directory: str
    command_shape: tuple[str, ...]
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, object]:
        """投影为 private diagnostic artifact 的 JSON-safe payload。"""
        return {
            "process_exit_code": self.process_exit_code,
            "startup_failure_phase": self.startup_failure_phase,
            "interpreter_path": self.interpreter_path,
            "working_directory": self.working_directory,
            "command_shape": list(self.command_shape),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


class StatefulEnvironmentError(RuntimeError):
    """环境供给/绑定/清理失败（EVALUATION_INFRA_FAILURE）。"""

    def __init__(self, message: str, *, startup_diagnostic: StartupDiagnostic | None = None) -> None:
        super().__init__(message)
        self.startup_diagnostic = startup_diagnostic


@dataclass(frozen=True, slots=True)
class ScenarioEnvironmentEvidence:
    """一个 scenario 的隔离环境凭据（全部路径由 evaluation harness 拥有）。"""

    scenario_id: str
    scenario_environment_id: str
    scenario_token: str
    work_dir: Path
    memory_db_path: Path
    journal_db_path: Path
    target_instance_ref: str
    localagent_base_url: str | None
    fixture_seeded: bool
    provisioned_at: datetime
    evaluation_only_harness: bool = False
    localagent_python_executable_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("scenario_id", "scenario_environment_id", "scenario_token", "target_instance_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise StatefulEnvironmentError(f"{field_name} must be a non-empty string")


class StatefulEnvironmentProvisioner(Protocol):
    """为每个 Scenario 提供 fresh isolated Memory 环境并验证绑定。"""

    async def provision(self, scenario: StatefulMemoryScenario) -> ScenarioEnvironmentEvidence:
        """创建 fresh isolated DB/journal 并启动/绑定专属 instance。"""
        ...

    async def verify_bound(self, evidence: ScenarioEnvironmentEvidence) -> bool:
        """验证 LocalAgent instance 确实使用该 scenario 的 DB/journal（证据驱动）。"""
        ...

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> ExecutionTarget:
        """返回绑定到该隔离环境的 ExecutionTarget。"""
        ...

    async def cleanup(self, evidence: ScenarioEnvironmentEvidence, *, preserve: bool) -> None:
        """释放环境；preserve=True 时保留 DB/journal/state（FAIL/BLOCKED 必须保留）。"""
        ...


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scenario_token() -> str:
    return f"scn-{uuid4().hex[:12]}"


def _startup_tail(path: Path, *, secrets: tuple[str, ...]) -> str:
    """读取 startup file 的有限 tail，并在写 private artifact 前脱敏。"""
    try:
        data = path.read_bytes()[-MAX_STARTUP_DIAGNOSTIC_BYTES:]
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    return re.sub(r"://[^\s/@:]+:[^\s/@]+@", "://[REDACTED]@", text)


def _startup_diagnostic(
    *,
    process: asyncio.subprocess.Process | None,
    phase: str,
    interpreter: Path,
    work_dir: Path,
    command: tuple[str, ...],
    env: dict[str, str],
) -> StartupDiagnostic:
    """构造无 secret 的 startup diagnostic；仅 record command shape，不导出 env。"""
    secrets = (env.get("LOCAL_AGENT_REMOTE_API_KEY", ""),)
    return StartupDiagnostic(
        process_exit_code=process.returncode if process is not None else None,
        startup_failure_phase=phase,
        interpreter_path=str(interpreter),
        working_directory=str(work_dir),
        command_shape=command,
        stdout_tail=_startup_tail(work_dir / STARTUP_STDOUT_FILE, secrets=secrets),
        stderr_tail=_startup_tail(work_dir / STARTUP_STDERR_FILE, secrets=secrets),
    )


def _write_startup_diagnostic(work_dir: Path, diagnostic: StartupDiagnostic) -> None:
    (work_dir / STARTUP_DIAGNOSTIC_FILE).write_text(
        json.dumps(diagnostic.to_dict(), ensure_ascii=False), encoding="utf-8"
    )


def _write_scenario_token(work_dir: Path, evidence: ScenarioEnvironmentEvidence) -> None:
    token_path = work_dir / SCENARIO_TOKEN_FILE
    token_path.write_text(
        json.dumps(
            {
                "scenario_id": evidence.scenario_id,
                "scenario_environment_id": evidence.scenario_environment_id,
                "scenario_token": evidence.scenario_token,
                "memory_db": evidence.memory_db_path.name,
                "journal_db": evidence.journal_db_path.name,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _create_empty_db(db_path: Path, schema: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema)
        connection.commit()
    finally:
        connection.close()


_MEMORY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS long_term_memory ("
    "memory_id TEXT PRIMARY KEY, "
    "memory_type TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "agent_id TEXT NOT NULL, "
    "memory_scope TEXT NOT NULL, "
    "canonical_text TEXT NOT NULL, "
    "payload TEXT NOT NULL, "
    "logical_key TEXT, "
    "origin_type TEXT NOT NULL, "
    "origin_run_id TEXT NOT NULL, "
    "origin_exchange_id TEXT NOT NULL, "
    "origin_agent_id TEXT NOT NULL, "
    "origin_memory_scope TEXT NOT NULL, "
    "formation_method TEXT, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, "
    "superseded_by_memory_id TEXT"
    ")"
)

_JOURNAL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS runtime_event_journal ("
    "event_id TEXT NOT NULL, "
    "run_id TEXT NOT NULL, "
    "trace_id TEXT, "
    "sequence INTEGER NOT NULL, "
    "emitted_at TEXT, "
    "journaled_at TEXT, "
    "event_type TEXT NOT NULL, "
    "component TEXT, "
    "step_id TEXT, "
    "step_sequence INTEGER, "
    "span_id TEXT, "
    "parent_span_id TEXT, "
    "safe_payload TEXT, "
    "payload_digest TEXT, "
    "event_digest TEXT, "
    "PRIMARY KEY (run_id, sequence)"
    ")"
)


def seed_fixture_memory(
    db_path: Path,
    *,
    records: list[object],
    environment_id: str,
    strict_canonical: bool = False,
) -> None:
    """Layer 1 deterministic fixture seed（只能在任何 target invocation 前调用）。

    ``strict_canonical=True`` 是 V2 path：非 FORGOTTEN record 必须使用已验证的
    ``record.canonical_text``，绝不回落到 ``<logical_key>: <value>``；缺失即
    ``StatefulEnvironmentError``（应在 dataset validation 阶段已拦截，这里是 fail closed）。
    ``strict_canonical=False`` 是 V1 compatibility path：允许 legacy fallback。
    FORGOTTEN record 一律写唯一 tombstone（``[FORGOTTEN]`` + ``payload={}`` +
    ``superseded_by=None``），两路径共享同一 redaction 契约。
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_MEMORY_SCHEMA)
        for record in records:
            memory_id = getattr(record, "alias", None) or f"mem-{uuid4().hex[:8]}"
            status = getattr(record, "status", "ACTIVE")
            if status == "FORGOTTEN":
                payload = "{}"
                canonical_text = FORGET_TOMBSTONE_TEXT
                superseded_by = None
            else:
                payload = json.dumps({"value": record.value}, ensure_ascii=False)
                provided_text = getattr(record, "canonical_text", None)
                if strict_canonical:
                    if not provided_text or not str(provided_text).strip():
                        raise StatefulEnvironmentError(
                            "V2 SEEDED non-FORGOTTEN record requires a validated canonical_text; "
                            "the legacy <logical_key>: <value> fallback is not allowed"
                        )
                    canonical_text = str(provided_text).strip()
                else:
                    canonical_text = provided_text or f"{record.logical_key}: {record.value}"
                superseded_by = getattr(record, "superseded_by_alias", None)
            connection.execute(
                "INSERT OR REPLACE INTO long_term_memory ("
                "memory_id, memory_type, status, agent_id, memory_scope, canonical_text, "
                "payload, logical_key, origin_type, origin_run_id, origin_exchange_id, "
                "origin_agent_id, origin_memory_scope, formation_method, created_at, "
                "updated_at, superseded_by_memory_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    getattr(record, "memory_type", "SEMANTIC"),
                    status,
                    record.agent_id,
                    record.memory_scope,
                    canonical_text,
                    payload,
                    record.logical_key,
                    "FIXTURE_SEED",
                    environment_id,
                    environment_id,
                    record.agent_id,
                    record.memory_scope,
                    "fixture_seed",
                    getattr(record, "created_at", "2026-01-01T00:00:00+00:00"),
                    getattr(record, "updated_at", "2026-01-01T00:00:00+00:00"),
                    superseded_by,
                ),
            )
        connection.commit()
    finally:
        connection.close()


class LocalAgentSubprocessProvisioner:
    """以显式配置的 LocalAgent interpreter + per-process env vars 启动私有实例。

    该 provisioner 依赖一个可执行的 LocalAgent 仓库与显式 interpreter 配置。
    interpreter 来源：constructor ``localagent_python_executable`` 参数优先，
    其次 AgentEvalOps Settings ``LOCALAGENT_PYTHON_EXECUTABLE``；两者都缺失或路径
    不存在时 fail closed（``StatefulEnvironmentError`` = EVALUATION_INFRA_FAILURE），
    绝不静默 fallback 到 ``sys.executable``。
    """

    def __init__(
        self,
        *,
        localagent_repo: Path,
        base_work_dir: Path,
        localagent_python_executable: str | Path | None = None,
        health_timeout_seconds: float = 60.0,
        health_poll_seconds: float = 1.0,
        subprocess_environment: dict[str, str] | None = None,
    ) -> None:
        self._repo = Path(localagent_repo)
        self._base_work_dir = Path(base_work_dir)
        self._health_timeout = health_timeout_seconds
        self._health_poll = health_poll_seconds
        self._subprocess_environment = dict(subprocess_environment or {})
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._localagent_python_executable = self._resolve_interpreter(localagent_python_executable)

    @staticmethod
    def _resolve_interpreter(
        explicit: str | Path | None,
    ) -> Path | None:
        """显式参数 > AgentEvalOps Settings；绝不回落到 sys.executable。"""
        raw = str(explicit) if explicit else ""
        if not raw:
            configured = getattr(settings, LOCALAGENT_PYTHON_EXECUTABLE_SETTING, "") or ""
            raw = configured
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def _require_interpreter(self) -> Path:
        interpreter = self._localagent_python_executable
        if interpreter is None:
            raise StatefulEnvironmentError(
                "localagent_python_executable is not configured; set the "
                f"{LOCALAGENT_PYTHON_EXECUTABLE_SETTING} setting or pass it explicitly; "
                "no fallback to sys.executable"
            )
        if not interpreter.is_file():
            raise StatefulEnvironmentError(f"configured localagent_python_executable does not exist: {interpreter}")
        return interpreter

    async def provision(
        self,
        scenario: StatefulMemoryScenario,
        *,
        extra_runtime_env_factory: Callable[[Path, int], Mapping[str, str]] | None = None,
    ) -> ScenarioEnvironmentEvidence:
        """创建 fresh isolated DB/journal 并以配置的 interpreter 启动私有实例。"""
        interpreter = self._require_interpreter()
        scenario_dir = self._base_work_dir / f"{scenario.scenario_id}-{uuid4().hex[:8]}"
        scenario_dir.mkdir(parents=True, exist_ok=True)
        memory_db = scenario_dir / SCENARIO_MEMORY_DB_FILE
        journal_db = scenario_dir / SCENARIO_JOURNAL_DB_FILE
        # LocalAgent startup 会先对已有 SQLite 文件执行严格 schema preflight，随后由
        # 自己的 Store owner 创建 current schema。这里若预建 evaluation-side 简化表，
        # 会被 preflight 判为 unsupported，反而无法启动。只保留 scenario-owned 目录与
        # 明确路径；health 成功后 Settings/Store 已创建真实 DB/journal。

        port = _free_port()
        token = _scenario_token()
        now = datetime.now(UTC)
        env = dict(os.environ)
        env[LOCALAGENT_MEMORY_DB_ENV] = str(memory_db)
        env[LOCALAGENT_JOURNAL_DB_ENV] = str(journal_db)
        env[LOCALAGENT_PORT_ENV] = str(port)
        env[LOCALAGENT_HOST_ENV] = "127.0.0.1"
        env[LOCALAGENT_ENVIRONMENT_ID_ENV] = token
        env[LOCALAGENT_ENVIRONMENT_PROFILE_ENV] = "TEST"
        env.update(self._subprocess_environment)
        if extra_runtime_env_factory is not None:
            env.update(dict(extra_runtime_env_factory(scenario_dir, port)))
        command = (
            str(interpreter),
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        )
        try:
            with (
                (scenario_dir / STARTUP_STDOUT_FILE).open("wb") as stdout,
                (scenario_dir / STARTUP_STDERR_FILE).open("wb") as stderr,
            ):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self._repo),
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                )
        except OSError as exc:
            raise StatefulEnvironmentError(f"cannot launch LocalAgent instance: {exc}") from exc
        base_url = f"http://127.0.0.1:{port}"
        self._processes[token] = process
        evidence = ScenarioEnvironmentEvidence(
            scenario_id=scenario.scenario_id,
            scenario_environment_id=f"env-{uuid4().hex[:12]}",
            scenario_token=token,
            work_dir=scenario_dir,
            memory_db_path=memory_db,
            journal_db_path=journal_db,
            target_instance_ref=f"localagent-process-{port}",
            localagent_base_url=base_url,
            fixture_seeded=False,
            provisioned_at=now,
            evaluation_only_harness=False,
            localagent_python_executable_ref=str(interpreter),
        )
        try:
            await self._wait_healthy(
                base_url,
                process,
                interpreter=interpreter,
                scenario_dir=scenario_dir,
                command=command,
                env=env,
            )
        except Exception as exc:
            if isinstance(exc, StatefulEnvironmentError) and exc.startup_diagnostic is not None:
                _write_startup_diagnostic(scenario_dir, exc.startup_diagnostic)
            # health/binding 失败时此环境尚未交给 caller；必须在此处终止已启动的
            # subprocess 并清理 evaluation-owned DB，避免留下 orphan process/port。
            await self.cleanup(evidence, preserve=False)
            raise
        _write_scenario_token(scenario_dir, evidence)
        return evidence

    async def _wait_healthy(
        self,
        base_url: str,
        process: asyncio.subprocess.Process | None = None,
        *,
        interpreter: Path | None = None,
        scenario_dir: Path | None = None,
        command: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
    ) -> None:
        """轮询 /health；进程提前退出（interpreter 无法执行）时立即 fail closed。"""
        deadline = time.monotonic() + self._health_timeout
        # 此 client 只访问本 provisioner 刚启动的 loopback LocalAgent。不能继承
        # 用户/系统 proxy，否则 health binding 会被错误转发到外部 proxy。
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process is not None and process.returncode is not None:
                    diagnostic = (
                        _startup_diagnostic(
                            process=process,
                            phase="PROVISIONING_PROCESS_EXIT",
                            interpreter=interpreter,
                            work_dir=scenario_dir,
                            command=command,
                            env=env,
                        )
                        if interpreter is not None and scenario_dir is not None and env is not None
                        else None
                    )
                    raise StatefulEnvironmentError(
                        f"LocalAgent subprocess exited before becoming healthy "
                        f"(returncode={process.returncode}): {base_url}",
                        startup_diagnostic=diagnostic,
                    )
                try:
                    response = await client.get(f"{base_url}{HEALTH_PATH}")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self._health_poll)
        diagnostic = (
            _startup_diagnostic(
                process=process,
                phase="HEALTH_TIMEOUT",
                interpreter=interpreter,
                work_dir=scenario_dir,
                command=command,
                env=env,
            )
            if interpreter is not None and scenario_dir is not None and env is not None
            else None
        )
        raise StatefulEnvironmentError(
            f"LocalAgent instance did not become healthy: {base_url}", startup_diagnostic=diagnostic
        )

    async def verify_bound(self, evidence: ScenarioEnvironmentEvidence) -> bool:
        """证据驱动验证 instance 存活且 token/DB/journal 均属于该 scenario。"""
        process = self._processes.get(evidence.scenario_token)
        if process is None or process.returncode is not None:
            return False
        if not evidence.memory_db_path.is_file() or not evidence.journal_db_path.is_file():
            return False
        token_file = evidence.work_dir / SCENARIO_TOKEN_FILE
        if not token_file.is_file():
            return False
        try:
            payload = json.loads(token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if payload.get("scenario_token") != evidence.scenario_token:
            return False
        return True

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> ExecutionTarget:
        """返回绑定到该隔离实例的 ``LocalAgentHttpExecutionTarget``。"""
        from app.adapters.evaluation.http_localagent import (
            LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
            LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
            LOCALAGENT_HTTP_TARGET_ID,
            LOCALAGENT_HTTP_TARGET_KIND,
            LocalAgentHttpExecutionTarget,
        )
        from app.core.evaluation.execution import ExecutionTargetRef

        if evidence.localagent_base_url is None:
            raise StatefulEnvironmentError("subprocess evidence requires localagent_base_url")
        target_ref = ExecutionTargetRef(
            target_id=LOCALAGENT_HTTP_TARGET_ID,
            target_kind=LOCALAGENT_HTTP_TARGET_KIND,
            target_version_ref=LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
            config_ref=LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
        )
        return LocalAgentHttpExecutionTarget(target_ref, evidence.localagent_base_url)

    async def cleanup(self, evidence: ScenarioEnvironmentEvidence, *, preserve: bool) -> None:
        """终止实例；preserve=False 时删除 scenario DB/journal/token 文件。"""
        process = self._processes.pop(evidence.scenario_token, None)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                process.kill()
                await asyncio.gather(process.wait(), return_exceptions=True)
        if not preserve:
            try:
                for name in (
                    SCENARIO_MEMORY_DB_FILE,
                    SCENARIO_JOURNAL_DB_FILE,
                    SCENARIO_TOKEN_FILE,
                    STARTUP_STDOUT_FILE,
                    STARTUP_STDERR_FILE,
                ):
                    (evidence.work_dir / name).unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "HEALTH_PATH",
    "LOCALAGENT_ENVIRONMENT_ID_ENV",
    "LOCALAGENT_ENVIRONMENT_PROFILE_ENV",
    "LOCALAGENT_HOST_ENV",
    "LOCALAGENT_JOURNAL_DB_ENV",
    "LOCALAGENT_MEMORY_DB_ENV",
    "LOCALAGENT_PORT_ENV",
    "LOCALAGENT_PYTHON_EXECUTABLE_SETTING",
    "MAX_STARTUP_DIAGNOSTIC_BYTES",
    "LocalAgentSubprocessProvisioner",
    "SCENARIO_JOURNAL_DB_FILE",
    "SCENARIO_MEMORY_DB_FILE",
    "SCENARIO_TOKEN_FILE",
    "ScenarioEnvironmentEvidence",
    "STARTUP_DIAGNOSTIC_FILE",
    "STARTUP_STDERR_FILE",
    "STARTUP_STDOUT_FILE",
    "StartupDiagnostic",
    "StatefulEnvironmentError",
    "StatefulEnvironmentProvisioner",
    "seed_fixture_memory",
]
