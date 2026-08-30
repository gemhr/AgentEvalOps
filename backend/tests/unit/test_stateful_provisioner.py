"""Stateful environment provisioner：interpreter config / env 继承 / 只读验证。"""

# ruff: noqa: D101, D105, D415

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from app.core.evaluation.stateful_memory_dataset import (
    MemoryRecordExpectation,
    StatefulMemoryScenario,
)
from app.services.evaluation import stateful_environment as env_module
from app.services.evaluation.stateful_environment import (
    LOCALAGENT_ENVIRONMENT_ID_ENV,
    LOCALAGENT_ENVIRONMENT_PROFILE_ENV,
    LOCALAGENT_HOST_ENV,
    LOCALAGENT_JOURNAL_DB_ENV,
    LOCALAGENT_MEMORY_DB_ENV,
    LOCALAGENT_PORT_ENV,
    LOCALAGENT_PYTHON_EXECUTABLE_SETTING,
    LocalAgentSubprocessProvisioner,
    ScenarioEnvironmentEvidence,
    StatefulEnvironmentError,
    seed_fixture_memory,
)


class FakeProcess:
    """最小 asyncio.subprocess.Process stand-in（provision 只读取 returncode）。"""

    returncode = None

    async def wait(self) -> int:
        """等待子进程退出。"""
        return 0

    def terminate(self) -> None:
        """终止子进程（no-op）。"""
        return None

    def kill(self) -> None:
        """强制终止子进程（no-op）。"""
        return None


def minimal_scenario():
    return StatefulMemoryScenario.model_validate(
        {
            "scenario_id": "scn",
            "description": "d",
            "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
            "tags": [],
            "initial_state": {"kind": "EMPTY"},
            "steps": [
                {
                    "step_id": "r1",
                    "agent_id": "core_router",
                    "memory_scope": "direct",
                    "query": "项目数据库使用 SQLite",
                }
            ],
        }
    )


def capture_subprocess(monkeypatch, tmp_path, *, interpreter):
    """Monkeypatch 捕获 subprocess 启动参数/env；返回 (provisioner, captured)。"""
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    async def fake_healthy(self, base_url, process=None, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(LocalAgentSubprocessProvisioner, "_wait_healthy", fake_healthy)
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        localagent_python_executable=interpreter,
        health_timeout_seconds=5.0,
    )
    return provisioner, captured


# ---------------------------------------------------------------- T1 / T2


def test_t1_configured_interpreter_is_used_not_sys_executable(monkeypatch, tmp_path):
    """配置的 interpreter 必须用于 subprocess，且与 sys.executable 不同。"""
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"fake")
    provisioner, captured = capture_subprocess(monkeypatch, tmp_path, interpreter=interpreter)
    resolved = Path(interpreter).expanduser().resolve()
    assert resolved != Path(sys.executable).expanduser().resolve()
    assert provisioner._localagent_python_executable == resolved

    import asyncio as _asyncio

    async def run():
        return await provisioner.provision(minimal_scenario())

    evidence = _asyncio.run(run())
    args = captured["args"]
    assert args[0] == str(resolved)
    assert args[1:5] == ("-m", "uvicorn", "server:app", "--host")
    assert "127.0.0.1" in args
    assert evidence.localagent_python_executable_ref == str(resolved)


def test_t1_settings_fallback_used_when_no_explicit_param(monkeypatch, tmp_path):
    interpreter = tmp_path / "settings-python.exe"
    interpreter.write_bytes(b"fake")
    monkeypatch.setattr(env_module.settings, LOCALAGENT_PYTHON_EXECUTABLE_SETTING, str(interpreter))
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return FakeProcess()

    async def fake_healthy(self, base_url, process=None, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(LocalAgentSubprocessProvisioner, "_wait_healthy", fake_healthy)
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        health_timeout_seconds=5.0,
    )
    resolved = Path(interpreter).expanduser().resolve()
    assert provisioner._localagent_python_executable == resolved

    import asyncio as _asyncio

    _asyncio.run(provisioner.provision(minimal_scenario()))
    assert captured["args"][0] == str(resolved)


def test_t2_missing_interpreter_fails_closed_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(env_module.settings, LOCALAGENT_PYTHON_EXECUTABLE_SETTING, "")
    spawned = {"called": False}

    async def fake_exec(*args, **kwargs):
        spawned["called"] = True
        return FakeProcess()

    async def fake_healthy(self, base_url, process=None, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(LocalAgentSubprocessProvisioner, "_wait_healthy", fake_healthy)
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        health_timeout_seconds=5.0,
    )
    with pytest.raises(StatefulEnvironmentError, match="not configured"):
        asyncio.run(provisioner.provision(minimal_scenario()))
    assert spawned["called"] is False


def test_t2_nonexistent_interpreter_fails_closed_no_fallback(monkeypatch, tmp_path):
    missing = tmp_path / "missing-python.exe"
    provisioner, captured = capture_subprocess(monkeypatch, tmp_path, interpreter=missing)
    with pytest.raises(StatefulEnvironmentError, match="does not exist"):
        asyncio.run(provisioner.provision(minimal_scenario()))
    assert "args" not in captured


# ---------------------------------------------------------------- T3


def test_t3_scenario_env_overrides_are_applied(monkeypatch, tmp_path):
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"fake")
    provisioner, captured = capture_subprocess(monkeypatch, tmp_path, interpreter=interpreter)

    async def run():
        return await provisioner.provision(minimal_scenario())

    evidence = asyncio.run(run())
    env = captured["kwargs"]["env"]
    assert env[LOCALAGENT_MEMORY_DB_ENV] == str(evidence.memory_db_path)
    assert env[LOCALAGENT_JOURNAL_DB_ENV] == str(evidence.journal_db_path)
    port = evidence.localagent_base_url.rsplit(":", 1)[1]
    assert env[LOCALAGENT_PORT_ENV] == port
    assert env[LOCALAGENT_ENVIRONMENT_ID_ENV] == evidence.scenario_token
    assert env[LOCALAGENT_ENVIRONMENT_PROFILE_ENV] == "TEST"
    assert env[LOCALAGENT_HOST_ENV] == "127.0.0.1"
    assert str(evidence.memory_db_path).startswith(str(tmp_path / "work"))


# ---------------------------------------------------------------- T4


def test_t4_parent_provider_env_is_inherited(monkeypatch, tmp_path):
    fake_key = "sk-test-fake-key-for-inheritance"
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_MODEL_NAME", "deepseek-v4-flash")
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_KEY", fake_key)
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"fake")
    provisioner, captured = capture_subprocess(monkeypatch, tmp_path, interpreter=interpreter)

    async def run():
        return await provisioner.provision(minimal_scenario())

    asyncio.run(run())
    env = captured["kwargs"]["env"]
    assert env["LOCAL_AGENT_REMOTE_API_BASE_URL"] == "https://api.deepseek.com"
    assert env["LOCAL_AGENT_REMOTE_MODEL_NAME"] == "deepseek-v4-flash"
    # 只验证 key 已继承且非空；绝不打印 actual value
    assert env.get("LOCAL_AGENT_REMOTE_API_KEY", "") == fake_key
    assert len(env["LOCAL_AGENT_REMOTE_API_KEY"]) > 0


def test_t4_scenario_override_wins_over_parent_env(monkeypatch, tmp_path):
    monkeypatch.setenv(LOCALAGENT_MEMORY_DB_ENV, "C:/parent/should-not-win.db")
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"fake")
    provisioner, captured = capture_subprocess(monkeypatch, tmp_path, interpreter=interpreter)

    async def run():
        return await provisioner.provision(minimal_scenario())

    evidence = asyncio.run(run())
    env = captured["kwargs"]["env"]
    assert env[LOCALAGENT_MEMORY_DB_ENV] == str(evidence.memory_db_path)
    assert str(evidence.memory_db_path) != "C:/parent/should-not-win.db"


def test_env_var_names_match_localagent_settings_contract():
    # 必须与 LocalAgent settings.py 的 env 变量一致（read-only contract）
    assert LOCALAGENT_MEMORY_DB_ENV == "LOCAL_AGENT_MEMORY_DB_PATH"
    assert LOCALAGENT_JOURNAL_DB_ENV == "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH"
    assert LOCALAGENT_PORT_ENV == "LOCAL_AGENT_API_PORT"
    assert LOCALAGENT_ENVIRONMENT_ID_ENV == "LOCAL_AGENT_ENVIRONMENT_ID"
    assert LOCALAGENT_PYTHON_EXECUTABLE_SETTING == "LOCALAGENT_PYTHON_EXECUTABLE"


def test_scenario_environment_evidence_requires_identities():
    with pytest.raises(StatefulEnvironmentError):
        ScenarioEnvironmentEvidence(
            scenario_id="",
            scenario_environment_id="env",
            scenario_token="token",
            work_dir=Path("."),
            memory_db_path=Path("m.db"),
            journal_db_path=Path("j.db"),
            target_instance_ref="ref",
            localagent_base_url=None,
            fixture_seeded=False,
            provisioned_at=None,
        )


def test_seed_fixture_memory_writes_redacted_forgotten_tombstone(tmp_path):
    db = tmp_path / "memory.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE long_term_memory (memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, "
        "status TEXT NOT NULL, agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, "
        "canonical_text TEXT NOT NULL, payload TEXT NOT NULL, logical_key TEXT, "
        "origin_type TEXT NOT NULL, origin_run_id TEXT NOT NULL, origin_exchange_id TEXT NOT NULL, "
        "origin_agent_id TEXT NOT NULL, origin_memory_scope TEXT NOT NULL, formation_method TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, superseded_by_memory_id TEXT)"
    )
    connection.commit()
    connection.close()
    seed = MemoryRecordExpectation.model_validate(
        {
            "alias": "forgone",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.legacy_database",
            "status": "FORGOTTEN",
        }
    )
    # Fixture writer 是最后一道 privacy/redaction boundary：即使调用方绕过 DTO
    # 约束注入 relation，也不得把 FORGOTTEN tombstone 连到任何原始 Memory。
    seed.superseded_by_alias = "must-not-survive"
    seed_fixture_memory(db, records=[seed], environment_id="env-1")
    connection = sqlite3.connect(db)
    row = connection.execute(
        "SELECT status, canonical_text, payload, superseded_by_memory_id FROM long_term_memory"
    ).fetchone()
    connection.close()
    assert row[0] == "FORGOTTEN"
    assert row[1] == "[FORGOTTEN]"
    assert row[2] == "{}"
    assert row[3] is None


def test_seed_fixture_memory_maps_alias_as_memory_id_and_supersede_relation(tmp_path):
    db = tmp_path / "memory.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE long_term_memory (memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, "
        "status TEXT NOT NULL, agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, "
        "canonical_text TEXT NOT NULL, payload TEXT NOT NULL, logical_key TEXT, "
        "origin_type TEXT NOT NULL, origin_run_id TEXT NOT NULL, origin_exchange_id TEXT NOT NULL, "
        "origin_agent_id TEXT NOT NULL, origin_memory_scope TEXT NOT NULL, formation_method TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, superseded_by_memory_id TEXT)"
    )
    connection.commit()
    connection.close()
    old = MemoryRecordExpectation.model_validate(
        {
            "alias": "db_old",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "SUPERSEDED",
            "value": "SQLite",
            "superseded_by_alias": "db_new",
        }
    )
    new = MemoryRecordExpectation.model_validate(
        {
            "alias": "db_new",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "PostgreSQL",
        }
    )
    seed_fixture_memory(db, records=[old, new], environment_id="env-1")
    connection = sqlite3.connect(db)
    rows = dict(connection.execute("SELECT memory_id, superseded_by_memory_id FROM long_term_memory").fetchall())
    connection.close()
    assert rows["db_old"] == "db_new"
    assert rows["db_new"] is None


def test_seeded_scenario_without_records_rejected():
    with pytest.raises(ValueError):
        StatefulMemoryScenario.model_validate(
            {
                "scenario_id": "s",
                "description": "d",
                "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
                "initial_state": {"kind": "SEEDED", "records": []},
                "steps": [
                    {
                        "step_id": "r1",
                        "agent_id": "a",
                        "memory_scope": "direct",
                        "query": "q",
                    }
                ],
            }
        )


# ------------------------------------------------------------------ E1-R3 V2 strict seed


def _seed_table(db: Path) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE long_term_memory (memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, "
        "status TEXT NOT NULL, agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, "
        "canonical_text TEXT NOT NULL, payload TEXT NOT NULL, logical_key TEXT, "
        "origin_type TEXT NOT NULL, origin_run_id TEXT NOT NULL, origin_exchange_id TEXT NOT NULL, "
        "origin_agent_id TEXT NOT NULL, origin_memory_scope TEXT NOT NULL, formation_method TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, superseded_by_memory_id TEXT)"
    )
    connection.commit()
    connection.close()


def test_v2_strict_seed_requires_canonical_text_no_legacy_fallback(tmp_path):
    from app.core.evaluation.stateful_memory_dataset_v2 import SeededMemoryRecord

    db = tmp_path / "memory.db"
    _seed_table(db)
    record = SeededMemoryRecord.model_validate(
        {
            "alias": "db",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
            "canonical_text": "  项目数据库使用 SQLite  ",
        }
    )
    seed_fixture_memory(db, records=[record], environment_id="env-1", strict_canonical=True)
    connection = sqlite3.connect(db)
    row = connection.execute(
        "SELECT canonical_text, payload, logical_key FROM long_term_memory WHERE memory_id='db'"
    ).fetchone()
    connection.close()
    # trimmed canonical_text 写入，绝不用 <logical_key>: <value> fallback
    assert row[0] == "项目数据库使用 SQLite"
    assert row[1] == '{"value": "SQLite"}'
    assert row[2] == "project.database"


def test_v2_strict_seed_fails_closed_when_canonical_text_missing(tmp_path):
    db = tmp_path / "memory.db"
    _seed_table(db)
    # V2 dataset validation 会拦截，但 seed helper 仍需 fail closed（不 fallback）
    stub = type(
        "Stub",
        (),
        {
            "alias": "db",
            "status": "ACTIVE",
            "value": "SQLite",
            "logical_key": "project.database",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "canonical_text": None,
            "superseded_by_alias": None,
        },
    )()
    with pytest.raises(StatefulEnvironmentError, match="fallback is not allowed"):
        seed_fixture_memory(db, records=[stub], environment_id="env-1", strict_canonical=True)


def test_v1_legacy_seed_keeps_fallback_in_non_strict_mode(tmp_path):
    db = tmp_path / "memory.db"
    _seed_table(db)
    record = MemoryRecordExpectation.model_validate(
        {
            "alias": "db",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
        }
    )
    seed_fixture_memory(db, records=[record], environment_id="env-1", strict_canonical=False)
    connection = sqlite3.connect(db)
    row = connection.execute("SELECT canonical_text FROM long_term_memory WHERE memory_id='db'").fetchone()
    connection.close()
    assert row[0] == "project.database: SQLite"
