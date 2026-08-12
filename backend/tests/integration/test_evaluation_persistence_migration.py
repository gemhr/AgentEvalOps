"""Alembic WP3 disposable PostgreSQL migration verification。"""

# ruff: noqa: D415

import os
import re
import subprocess
from dataclasses import dataclass
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """覆盖 integration conftest 的 create_all；本模块只允许 Alembic 建表。"""
    yield


@pytest.fixture(autouse=True)
def _override_deps():
    """覆盖 API/Redis autouse fixture，避免 migration test 依赖普通 integration DB。"""
    yield


@pytest.fixture
def migration_database():
    """Create a disposable database on the integration PostgreSQL instance。"""
    name = f"pandaprobe_wp3_{uuid4().hex}"
    admin = psycopg2.connect(host="localhost", port=5433, user="postgres", password="postgres", dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with admin.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
            cursor.execute(f'DROP DATABASE "{name}"')
        admin.close()


def alembic(database: str, *args: str) -> None:
    env = os.environ.copy()
    env.update({"POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5433", "POSTGRES_DB": database})
    subprocess.run(["uv", "run", "--frozen", "--no-sync", "alembic", *args], check=True, env=env)


def _assert_columns(inspector, table: str, expected: dict[str, bool]) -> None:
    columns = {item["name"]: item for item in inspector.get_columns(table)}
    assert set(columns) == set(expected)
    assert {name: item["nullable"] for name, item in columns.items()} == expected


def _assert_named_columns(
    items, expected: dict[str, tuple[str, ...]], *, column_key: str = "column_names"
) -> None:
    actual = {item["name"]: tuple(item[column_key]) for item in items}
    for name, columns in expected.items():
        assert actual[name] == columns


def _normalize_sql(value: str) -> str:
    """保留括号、操作符和值，仅消除 PostgreSQL 展示层 casts/空白差异。"""
    normalized = value.lower().replace('"', "")
    normalized = re.sub(
        r"::(?:character varying|double precision|float8|text|varchar|bpchar)(?:\[\])?",
        "",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized).strip()


_SQL_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:''|[^'])*')|(?P<number>\d+(?:\.\d+)?)|"
    r"(?P<operator><>|>=|=|>)|(?P<identifier>[a-z_][a-z0-9_$.]*)|(?P<punct>[(),\[\]]))",
    re.IGNORECASE,
)


@dataclass
class _BooleanExpressionParser:
    """解析当前 WP3 CHECK/index predicate 使用的有限 SQL 布尔 grammar。"""

    tokens: list[str]
    offset: int = 0

    def parse(self):
        """返回保留 AND/OR 类型与嵌套关系的 canonical tuple。"""
        if self._peek() == "check":
            self._take("check")
        expression = self._parse_or()
        assert self.offset == len(self.tokens), f"unparsed SQL tokens: {self.tokens[self.offset:]}"
        return expression

    def _peek(self) -> str | None:
        return None if self.offset == len(self.tokens) else self.tokens[self.offset]

    def _take(self, expected: str | None = None) -> str:
        assert self.offset < len(self.tokens), f"expected {expected or 'token'}, reached end"
        token = self.tokens[self.offset]
        if expected is not None:
            assert token == expected, f"expected {expected!r}, got {token!r}"
        self.offset += 1
        return token

    def _parse_or(self):
        children = [self._parse_and()]
        while self._peek() == "or":
            self._take("or")
            children.append(self._parse_and())
        return _logical_node("or", children)

    def _parse_and(self):
        children = [self._parse_not()]
        while self._peek() == "and":
            self._take("and")
            children.append(self._parse_not())
        return _logical_node("and", children)

    def _parse_not(self):
        if self._peek() == "not":
            self._take("not")
            return ("not", self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self):
        left = self._parse_primary()
        if self._peek() == "is":
            self._take("is")
            negated = self._peek() == "not"
            if negated:
                self._take("not")
            self._take("null")
            return ("is_not_null" if negated else "is_null", left)
        negated_in = self._peek() == "not" and self._following() == "in"
        if self._peek() == "in" or negated_in:
            if negated_in:
                self._take("not")
            self._take("in")
            values = self._parse_list()
            return ("not_in" if negated_in else "membership", left, _canonical_values(values))
        if self._peek() in {"=", "<>", ">", ">="}:
            operator = self._take()
            if operator == "=" and self._peek() == "any":
                return ("membership", left, _canonical_values(self._parse_any_array()))
            return (operator, left, self._parse_primary())
        return left

    def _following(self) -> str | None:
        return None if self.offset + 1 >= len(self.tokens) else self.tokens[self.offset + 1]

    def _parse_primary(self):
        if self._peek() == "(":
            self._take("(")
            value = self._parse_or()
            self._take(")")
            return value
        token = self._take()
        if self._peek() == "(":
            self._take("(")
            arguments = []
            if self._peek() != ")":
                arguments.append(self._parse_or())
                while self._peek() == ",":
                    self._take(",")
                    arguments.append(self._parse_or())
            self._take(")")
            return ("call", token, tuple(arguments))
        if token.startswith("'"):
            return ("literal", token[1:-1].replace("''", "'"))
        if token[0].isdigit():
            return ("number", token)
        return ("identifier", token)

    def _parse_list(self) -> list[tuple]:
        self._take("(")
        values = [self._parse_primary()]
        while self._peek() == ",":
            self._take(",")
            values.append(self._parse_primary())
        self._take(")")
        return values

    def _parse_any_array(self) -> list[tuple]:
        self._take("any")
        self._take("(")
        wrappers = 0
        while self._peek() == "(":
            self._take("(")
            wrappers += 1
        self._take("array")
        self._take("[")
        values = [self._parse_primary()]
        while self._peek() == ",":
            self._take(",")
            values.append(self._parse_primary())
        self._take("]")
        for _ in range(wrappers):
            self._take(")")
        self._take(")")
        return values


def _logical_node(operator: str, children: list[tuple]):
    """只 flatten 相同 operator；AND/OR 不能互相折叠。"""
    flattened = []
    for child in children:
        if child[0] == operator:
            flattened.extend(child[1])
        else:
            flattened.append(child)
    if len(flattened) == 1:
        return flattened[0]
    return (operator, tuple(sorted(flattened, key=repr)))


def _canonical_values(values: list[tuple]) -> tuple:
    return tuple(sorted(values, key=repr))


def _tokenize_boolean_sql(value: str) -> list[str]:
    normalized = _normalize_sql(value)
    tokens = []
    offset = 0
    while offset < len(normalized):
        match = _SQL_TOKEN.match(normalized, offset)
        assert match is not None, f"unsupported SQL near {normalized[offset:]!r}"
        tokens.append(match.group(0).strip())
        offset = match.end()
    return tokens


def _boolean_tree(value: str):
    """将 catalog CHECK/predicate 转为有限、可比较的布尔表达式树。"""
    return _BooleanExpressionParser(_tokenize_boolean_sql(value)).parse()


def _assert_boolean_semantics(actual: str, expected: str) -> None:
    assert _boolean_tree(actual) == _boolean_tree(expected)


def _assert_ordered(definition: str, *tokens: str) -> None:
    """要求语义 token 按顺序存在，同时保留 expression 的原始逻辑分组。"""
    offset = 0
    for token in tokens:
        offset = definition.find(token, offset)
        assert offset >= 0, f"missing ordered token {token!r} in {definition!r}"
        offset += len(token)


def _quoted_values(value: str) -> set[str]:
    """提取 CHECK/index predicate 中的实际字符串枚举成员。"""
    return set(re.findall(r"'([^']+)'", value))


def _get_check_definitions(engine, table: str) -> dict[str, str]:
    """从 PostgreSQL catalog 获取不会丢失 expression 的 CHECK definitions。"""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT constraint_row.conname, pg_get_constraintdef(constraint_row.oid, true)
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
                JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
                WHERE constraint_row.contype = 'c'
                  AND schema_row.nspname = current_schema()
                  AND table_row.relname = :table
                """
            ),
            {"table": table},
        ).all()
    return {name: definition for name, definition in rows}


def _assert_enum_check(
    checks: dict[str, str], name: str, column: str, expected_values: set[str]
) -> None:
    definition = checks[name]
    assert column in _normalize_sql(definition)
    assert _quoted_values(definition) == expected_values


def _assert_pair_check(checks: dict[str, str], name: str, kind: str, value: str) -> None:
    definition = _normalize_sql(checks[name])
    _assert_ordered(definition, kind, "is null", "=", value, "is null")


def _assert_fk(
    inspector,
    table: str,
    name: str,
    constrained: tuple[str, ...],
    referred_table: str,
    referred: tuple[str, ...],
    ondelete: str | None,
) -> None:
    foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys(table)}
    actual = foreign_keys[name]
    assert tuple(actual["constrained_columns"]) == constrained
    assert actual["referred_table"] == referred_table
    assert tuple(actual["referred_columns"]) == referred
    assert actual.get("options", {}).get("ondelete") == ondelete


def _index_predicate(index: dict[str, object]) -> str:
    return str(index["dialect_options"]["postgresql_where"])


RETRY_LINEAGE_SEMANTICS = (
    "(attempt_no = 1 AND retry_of_attempt_id IS NULL) OR "
    "(attempt_no > 1 AND retry_of_attempt_id IS NOT NULL)"
)
CLAIM_STATE_SEMANTICS = (
    "(status = 'PENDING' AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
    "(status IN ('CLAIMED','RUNNING') AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
    "AND lease_expires_at IS NOT NULL) OR (status = 'TERMINAL' AND claim_token IS NOT NULL)"
)
OUTCOME_PAYLOAD_SEMANTICS = (
    "execution_outcome_kind IS NULL OR "
    "(execution_outcome_kind = 'SUCCESS' AND output_artifact_ref IS NOT NULL AND error_category IS NULL) OR "
    "(execution_outcome_kind <> 'SUCCESS' AND output_artifact_ref IS NULL "
    "AND error_category IS NOT NULL AND reason IS NOT NULL)"
)
STALE_INDEX_SEMANTICS = "status IN ('CLAIMED','RUNNING')"


def test_semantic_helper_accepts_postgresql_catalog_display_variants():
    """Catalog 的 CHECK wrapper、冗余括号、casts 与 ANY display 不改变结构语义。"""
    _assert_boolean_semantics(
        "CHECK ((((attempt_no = 1) AND (retry_of_attempt_id IS NULL)) OR "
        "((attempt_no > 1) AND (retry_of_attempt_id IS NOT NULL))))",
        RETRY_LINEAGE_SEMANTICS,
    )
    _assert_boolean_semantics(
        "((status)::text = ANY ((ARRAY['RUNNING'::character varying, "
        "'CLAIMED'::character varying])::text[]))",
        STALE_INDEX_SEMANTICS,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "(attempt_no = 1 OR retry_of_attempt_id IS NULL) OR "
        "(attempt_no > 1 AND retry_of_attempt_id IS NOT NULL)",
        "(attempt_no = 1 AND retry_of_attempt_id IS NULL) OR "
        "(attempt_no > 1 OR retry_of_attempt_id IS NOT NULL)",
        "(attempt_no = 1 AND retry_of_attempt_id IS NULL) AND "
        "(attempt_no > 1 AND retry_of_attempt_id IS NOT NULL)",
    ],
)
def test_semantic_helper_rejects_retry_lineage_operator_mutations(mutation):
    """合法 tree 可解析，但分支或顶层 operator mutation 必须被拒绝。"""
    _assert_boolean_semantics(RETRY_LINEAGE_SEMANTICS, RETRY_LINEAGE_SEMANTICS)
    with pytest.raises(AssertionError):
        _assert_boolean_semantics(mutation, RETRY_LINEAGE_SEMANTICS)


@pytest.mark.parametrize(
    "mutation",
    [
        "(status = 'PENDING' OR claim_token IS NULL OR claimed_at IS NULL OR lease_expires_at IS NULL) OR "
        "(status IN ('CLAIMED','RUNNING') AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR (status = 'TERMINAL' AND claim_token IS NOT NULL)",
        "(status = 'PENDING' AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
        "(status IN ('CLAIMED','RUNNING') OR claim_token IS NOT NULL OR claimed_at IS NOT NULL "
        "OR lease_expires_at IS NOT NULL) OR (status = 'TERMINAL' AND claim_token IS NOT NULL)",
    ],
)
def test_semantic_helper_rejects_claim_state_operator_mutations(mutation):
    """PENDING 与 CLAIMED/RUNNING 分支的 AND 均不可退化为 OR。"""
    _assert_boolean_semantics(CLAIM_STATE_SEMANTICS, CLAIM_STATE_SEMANTICS)
    with pytest.raises(AssertionError):
        _assert_boolean_semantics(mutation, CLAIM_STATE_SEMANTICS)


@pytest.mark.parametrize(
    "mutation",
    [
        "execution_outcome_kind IS NULL OR "
        "(execution_outcome_kind = 'SUCCESS' AND output_artifact_ref IS NOT NULL OR error_category IS NULL) OR "
        "(execution_outcome_kind <> 'SUCCESS' AND output_artifact_ref IS NULL "
        "AND error_category IS NOT NULL AND reason IS NOT NULL)",
        "execution_outcome_kind IS NULL OR "
        "(execution_outcome_kind = 'SUCCESS' AND output_artifact_ref IS NOT NULL AND error_category IS NULL) OR "
        "(execution_outcome_kind <> 'SUCCESS' AND output_artifact_ref IS NULL "
        "OR error_category IS NOT NULL OR reason IS NOT NULL)",
    ],
)
def test_semantic_helper_rejects_outcome_payload_operator_mutations(mutation):
    """SUCCESS 与 non-SUCCESS payload 分支必须保持各自的 conjunction。"""
    _assert_boolean_semantics(OUTCOME_PAYLOAD_SEMANTICS, OUTCOME_PAYLOAD_SEMANTICS)
    with pytest.raises(AssertionError):
        _assert_boolean_semantics(mutation, OUTCOME_PAYLOAD_SEMANTICS)


@pytest.mark.parametrize(
    "mutation",
    [
        "status = 'CLAIMED' AND status = 'RUNNING'",
        "status = 'CLAIMED' OR status = 'PENDING'",
        "status IN ('CLAIMED')",
    ],
)
def test_semantic_helper_rejects_stale_membership_mutations(mutation):
    """Stale predicate 必须是两个目标状态的 membership，而非 token/value coincidence。"""
    _assert_boolean_semantics(
        "status = ANY (ARRAY['RUNNING'::text, 'CLAIMED'::text])",
        STALE_INDEX_SEMANTICS,
    )
    with pytest.raises(AssertionError):
        _assert_boolean_semantics(mutation, STALE_INDEX_SEMANTICS)


def _assert_schema_parity(engine) -> None:
    inspector = inspect(engine)
    assert {"evaluation_runs", "evaluation_attempts", "evaluation_results"} <= set(inspector.get_table_names())

    _assert_columns(inspector, "evaluation_runs", {
        "id": False, "project_id": False, "dataset_id": False, "dataset_version": False,
        "suite_id": False, "suite_version": False, "execution_target_id": False,
        "execution_target_kind": False, "target_version_kind": True, "target_version_value": True,
        "dataset_snapshot": False, "suite_snapshot": False, "execution_target_snapshot": False,
        "subject_ref": True, "status": False, "status_reason": True, "metadata": False,
        "created_at": False, "started_at": True, "finished_at": True,
    })
    run_columns = {item["name"]: item for item in inspector.get_columns("evaluation_runs")}
    assert "{}" in run_columns["metadata"]["default"]
    assert "CURRENT_TIMESTAMP" in run_columns["created_at"]["default"].upper()
    _assert_named_columns(inspector.get_unique_constraints("evaluation_runs"), {
        "uq_evaluation_runs_project_id_id": ("project_id", "id"),
        "uq_evaluation_runs_result_provenance": (
            "project_id", "id", "dataset_id", "dataset_version", "suite_id", "suite_version",
        ),
    })
    _assert_fk(
        inspector, "evaluation_runs", "fk_evaluation_runs_project", ("project_id",),
        "projects", ("id",), "CASCADE",
    )
    run_checks = _get_check_definitions(engine, "evaluation_runs")
    assert set(run_checks) == {
        "ck_evaluation_runs_status", "ck_evaluation_runs_target_version_pair",
        "ck_evaluation_runs_terminal_finished", "ck_evaluation_runs_finished_order",
    }
    _assert_enum_check(
        run_checks, "ck_evaluation_runs_status", "status",
        {"PENDING", "RUNNING", "COMPLETED", "FAILED", "OUTCOME_UNKNOWN"},
    )
    _assert_pair_check(
        run_checks, "ck_evaluation_runs_target_version_pair", "target_version_kind", "target_version_value"
    )
    terminal_finished = _normalize_sql(run_checks["ck_evaluation_runs_terminal_finished"])
    assert _quoted_values(run_checks["ck_evaluation_runs_terminal_finished"]) == {
        "COMPLETED", "FAILED", "OUTCOME_UNKNOWN",
    }
    _assert_ordered(terminal_finished, "status", "=", "finished_at", "is not null")
    finished_order = _normalize_sql(run_checks["ck_evaluation_runs_finished_order"])
    _assert_ordered(
        finished_order, "finished_at", "is null", "or", "finished_at", ">=", "coalesce",
        "started_at", "created_at",
    )
    _assert_named_columns(inspector.get_indexes("evaluation_runs"), {
        "ix_evaluation_runs_project_created": ("project_id", "created_at"),
        "ix_evaluation_runs_project_status_created": ("project_id", "status", "created_at"),
    })

    _assert_columns(inspector, "evaluation_attempts", {
        "id": False, "project_id": False, "run_id": False, "case_id": False, "case_version": False,
        "attempt_no": False, "retry_of_attempt_id": True, "execution_target_id": False,
        "execution_target_kind": False, "target_version_kind": True, "target_version_value": True,
        "target_config_kind": True, "target_config_value": True, "execution_request_id": False,
        "idempotency_key": False, "request_snapshot": False, "status": False, "claim_token": True,
        "worker_ref": True, "task_ref": True, "created_at": False, "claimed_at": True,
        "started_at": True, "finished_at": True, "lease_expires_at": True,
        "execution_outcome_kind": True, "output_artifact_ref": True, "outcome_evidence_refs": False,
        "error_category": True, "reason": True, "outcome_metadata": False,
    })
    attempt_columns = {item["name"]: item for item in inspector.get_columns("evaluation_attempts")}
    assert "[]" in attempt_columns["outcome_evidence_refs"]["default"]
    assert "{}" in attempt_columns["outcome_metadata"]["default"]
    assert "CURRENT_TIMESTAMP" in attempt_columns["created_at"]["default"].upper()
    _assert_named_columns(inspector.get_unique_constraints("evaluation_attempts"), {
        "uq_evaluation_attempts_project_run_id": ("project_id", "run_id", "id"),
        "uq_evaluation_attempts_result_provenance": (
            "project_id", "run_id", "id", "case_id", "case_version",
            "execution_target_id", "execution_request_id",
        ),
        "uq_evaluation_attempts_case_number": (
            "project_id", "run_id", "case_id", "case_version", "attempt_no",
        ),
        "uq_evaluation_attempts_request": ("project_id", "run_id", "execution_request_id"),
        "uq_evaluation_attempts_claim_token": ("claim_token",),
    })
    _assert_fk(
        inspector, "evaluation_attempts", "fk_evaluation_attempts_project_run",
        ("project_id", "run_id"), "evaluation_runs", ("project_id", "id"), "CASCADE",
    )
    _assert_fk(
        inspector, "evaluation_attempts", "fk_evaluation_attempts_retry_parent",
        ("project_id", "run_id", "retry_of_attempt_id"),
        "evaluation_attempts", ("project_id", "run_id", "id"), None,
    )
    attempt_checks = _get_check_definitions(engine, "evaluation_attempts")
    assert set(attempt_checks) == {
        "ck_evaluation_attempts_number_positive", "ck_evaluation_attempts_retry_lineage",
        "ck_evaluation_attempts_status", "ck_evaluation_attempts_target_version_pair",
        "ck_evaluation_attempts_target_config_pair", "ck_evaluation_attempts_claim_state",
        "ck_evaluation_attempts_terminal_outcome", "ck_evaluation_attempts_outcome_kind",
        "ck_evaluation_attempts_outcome_payload",
    }
    _assert_ordered(
        _normalize_sql(attempt_checks["ck_evaluation_attempts_number_positive"]), "attempt_no", ">", "0"
    )
    _assert_boolean_semantics(
        attempt_checks["ck_evaluation_attempts_retry_lineage"], RETRY_LINEAGE_SEMANTICS
    )
    _assert_enum_check(
        attempt_checks, "ck_evaluation_attempts_status", "status",
        {"PENDING", "CLAIMED", "RUNNING", "TERMINAL"},
    )
    _assert_pair_check(
        attempt_checks, "ck_evaluation_attempts_target_version_pair",
        "target_version_kind", "target_version_value",
    )
    _assert_pair_check(
        attempt_checks, "ck_evaluation_attempts_target_config_pair",
        "target_config_kind", "target_config_value",
    )
    _assert_boolean_semantics(
        attempt_checks["ck_evaluation_attempts_claim_state"], CLAIM_STATE_SEMANTICS
    )
    terminal_outcome = _normalize_sql(attempt_checks["ck_evaluation_attempts_terminal_outcome"])
    assert _quoted_values(attempt_checks["ck_evaluation_attempts_terminal_outcome"]) == {"TERMINAL"}
    _assert_ordered(
        terminal_outcome, "status", "'terminal'", "=", "execution_outcome_kind", "is not null",
        "finished_at", "is not null",
    )
    _assert_enum_check(
        attempt_checks, "ck_evaluation_attempts_outcome_kind", "execution_outcome_kind",
        {"SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED", "OUTCOME_UNKNOWN"},
    )
    _assert_boolean_semantics(
        attempt_checks["ck_evaluation_attempts_outcome_payload"], OUTCOME_PAYLOAD_SEMANTICS
    )
    attempt_indexes = {item["name"]: item for item in inspector.get_indexes("evaluation_attempts")}
    assert attempt_indexes["uq_evaluation_attempts_direct_retry"]["unique"] is True
    assert tuple(attempt_indexes["uq_evaluation_attempts_direct_retry"]["column_names"]) == (
        "project_id", "run_id", "retry_of_attempt_id",
    )
    direct_retry_predicate = _index_predicate(attempt_indexes["uq_evaluation_attempts_direct_retry"])
    assert _normalize_sql(direct_retry_predicate).strip("() ") == "retry_of_attempt_id is not null"
    stale_index = attempt_indexes["ix_evaluation_attempts_stale"]
    assert stale_index["unique"] is False
    _assert_boolean_semantics(_index_predicate(stale_index), STALE_INDEX_SEMANTICS)
    _assert_named_columns(inspector.get_indexes("evaluation_attempts"), {
        "ix_evaluation_attempts_case_number": (
            "project_id", "run_id", "case_id", "case_version", "attempt_no",
        ),
        "ix_evaluation_attempts_run_status": ("project_id", "run_id", "status"),
        "ix_evaluation_attempts_stale": ("lease_expires_at",),
    })

    _assert_columns(inspector, "evaluation_results", {
        "id": False, "project_id": False, "run_id": False, "attempt_id": False,
        "dataset_id": False, "dataset_version": False, "case_id": False, "case_version": False,
        "suite_id": False, "suite_version": False, "evaluator_id": False, "evaluator_version": False,
        "config_ref_kind": False, "config_ref_value": False, "prompt_ref_kind": True,
        "prompt_ref_value": True, "execution_target_id": False, "target_version_kind": True,
        "target_version_value": True, "execution_request_id": False, "verdict": False,
        "reason": False, "provenance_completeness": False, "output_artifact_ref": True,
        "score": True, "evidence_refs": False, "metadata": False, "created_at": False,
    })
    result_columns = {item["name"]: item for item in inspector.get_columns("evaluation_results")}
    assert "[]" in result_columns["evidence_refs"]["default"]
    assert "{}" in result_columns["metadata"]["default"]
    assert "CURRENT_TIMESTAMP" in result_columns["created_at"]["default"].upper()
    _assert_fk(
        inspector, "evaluation_results", "fk_evaluation_results_run_provenance",
        ("project_id", "run_id", "dataset_id", "dataset_version", "suite_id", "suite_version"),
        "evaluation_runs", ("project_id", "id", "dataset_id", "dataset_version", "suite_id", "suite_version"),
        "CASCADE",
    )
    _assert_fk(
        inspector, "evaluation_results", "fk_evaluation_results_attempt_provenance",
        (
            "project_id", "run_id", "attempt_id", "case_id", "case_version",
            "execution_target_id", "execution_request_id",
        ),
        "evaluation_attempts",
        (
            "project_id", "run_id", "id", "case_id", "case_version",
            "execution_target_id", "execution_request_id",
        ),
        "CASCADE",
    )
    _assert_named_columns(inspector.get_unique_constraints("evaluation_results"), {
        "uq_evaluation_results_logical_slot": (
            "run_id", "attempt_id", "case_id", "case_version", "evaluator_id", "evaluator_version",
        ),
    })
    result_checks = _get_check_definitions(engine, "evaluation_results")
    assert set(result_checks) == {
        "ck_evaluation_results_verdict", "ck_evaluation_results_provenance",
        "ck_evaluation_results_prompt_pair", "ck_evaluation_results_target_pair",
        "ck_evaluation_results_finite_score",
    }
    _assert_enum_check(
        result_checks, "ck_evaluation_results_verdict", "verdict",
        {"PASS", "FAIL", "INCONCLUSIVE", "ERROR"},
    )
    _assert_enum_check(
        result_checks, "ck_evaluation_results_provenance", "provenance_completeness",
        {"COMPLETE", "PARTIAL"},
    )
    assert result_columns["config_ref_kind"]["nullable"] is False
    assert result_columns["config_ref_value"]["nullable"] is False
    _assert_pair_check(
        result_checks, "ck_evaluation_results_prompt_pair", "prompt_ref_kind", "prompt_ref_value"
    )
    _assert_pair_check(
        result_checks, "ck_evaluation_results_target_pair", "target_version_kind", "target_version_value"
    )
    finite_score = _normalize_sql(result_checks["ck_evaluation_results_finite_score"])
    _assert_ordered(finite_score, "score", "is null", "or", "score")
    assert "not in" in finite_score or "<> all" in finite_score
    assert _quoted_values(result_checks["ck_evaluation_results_finite_score"]) == {
        "Infinity", "-Infinity", "NaN",
    }
    _assert_named_columns(inspector.get_indexes("evaluation_results"), {
        "ix_evaluation_results_run_created": ("project_id", "run_id", "created_at"),
        "ix_evaluation_results_attempt": ("project_id", "attempt_id"),
        "ix_evaluation_results_case_evaluator": (
            "project_id", "case_id", "case_version", "evaluator_id", "evaluator_version",
        ),
    })

    with engine.connect() as connection:
        trigger_definition, function_definition = connection.execute(text("""
            SELECT pg_get_triggerdef(t.oid), pg_get_functiondef(p.oid)
            FROM pg_trigger t
            JOIN pg_proc p ON p.oid = t.tgfoid
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE t.tgname = 'trg_evaluation_results_immutable'
              AND c.relname = 'evaluation_results'
              AND NOT t.tgisinternal
        """)).one()
        assert "BEFORE UPDATE ON" in trigger_definition
        assert "evaluation_results" in trigger_definition
        assert "EXECUTE FUNCTION" in trigger_definition
        assert "reject_evaluation_result_update()" in trigger_definition
        assert "CREATE OR REPLACE FUNCTION" in function_definition
        assert "reject_evaluation_result_update()" in function_definition
        assert "evaluation_results rows are immutable" in function_definition


def _assert_immutable_trigger(engine) -> None:
    with engine.connect() as connection:
        ids = [str(uuid4()) for _ in range(5)]
        organization_id, project_id, run_id, attempt_id, result_id = ids
        connection.execute(text("INSERT INTO organizations (id,name,created_at) VALUES (:id,'org',CURRENT_TIMESTAMP)"), {"id": organization_id})
        connection.execute(text("INSERT INTO projects (id,org_id,name,description,created_at) VALUES (:id,:org,'project','',CURRENT_TIMESTAMP)"), {"id": project_id, "org": organization_id})
        connection.execute(text("""
            INSERT INTO evaluation_runs
              (id,project_id,dataset_id,dataset_version,suite_id,suite_version,execution_target_id,
               execution_target_kind,target_version_kind,target_version_value,dataset_snapshot,suite_snapshot,
               execution_target_snapshot,status,metadata,created_at,started_at)
            VALUES (:run,:project,'dataset','d1','suite','s1','target','FIXTURE','git','abc',
                    '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'RUNNING','{}'::jsonb,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        """), {"run": run_id, "project": project_id})
        attempt_insert = text("""
            INSERT INTO evaluation_attempts
              (id,project_id,run_id,case_id,case_version,attempt_no,execution_target_id,execution_target_kind,
               target_version_kind,target_version_value,execution_request_id,idempotency_key,request_snapshot,status,
               claim_token,created_at,claimed_at,started_at,finished_at,lease_expires_at,execution_outcome_kind,
               output_artifact_ref,outcome_evidence_refs,outcome_metadata)
            VALUES (:attempt,:project,:run,'case','v1',1,'target','FIXTURE','git','abc','request','stable',
                    :request_snapshot,'TERMINAL',:token,CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'SUCCESS',
                    '{"artifact_id":"a"}'::jsonb,'[]'::jsonb,'{}'::jsonb)
        """).bindparams(bindparam("request_snapshot", type_=JSONB))
        connection.execute(
            attempt_insert,
            {
                "attempt": attempt_id,
                "project": project_id,
                "run": run_id,
                "token": str(uuid4()),
                "request_snapshot": {"input_payload": {}, "timeout_seconds": 1},
            },
        )
        connection.execute(text("""
            INSERT INTO evaluation_results
              (id,project_id,run_id,attempt_id,dataset_id,dataset_version,case_id,case_version,suite_id,suite_version,
               evaluator_id,evaluator_version,config_ref_kind,config_ref_value,execution_target_id,target_version_kind,
               target_version_value,execution_request_id,verdict,reason,provenance_completeness,evidence_refs,metadata,created_at)
            VALUES (:result,:project,:run,:attempt,'dataset','d1','case','v1','suite','s1','eval','e1','cfg','1',
                    'target','git','abc','request','FAIL','wrong','COMPLETE','[]'::jsonb,'{}'::jsonb,CURRENT_TIMESTAMP)
        """), {"result": result_id, "project": project_id, "run": run_id, "attempt": attempt_id})
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(text("UPDATE evaluation_results SET verdict='PASS' WHERE id=:id"), {"id": result_id})
        connection.rollback()


def test_empty_database_upgrade_head_and_schema_parity(migration_database):
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    _assert_schema_parity(engine)
    _assert_immutable_trigger(engine)
    engine.dispose()


def test_parent_upgrade_to_wp3_head(migration_database):
    alembic(migration_database, "upgrade", "3ca1d40bddfa")
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    assert "evaluation_results" in inspect(engine).get_table_names()
    engine.dispose()


def test_wp3_head_downgrade_parent_and_reupgrade(migration_database):
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    alembic(migration_database, "downgrade", "3ca1d40bddfa")
    assert "evaluation_runs" not in inspect(engine).get_table_names()
    alembic(migration_database, "upgrade", "head")
    assert "evaluation_results" in inspect(engine).get_table_names()
    engine.dispose()
