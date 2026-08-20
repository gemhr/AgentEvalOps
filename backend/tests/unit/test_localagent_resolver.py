"""Focused tests for the production LOCALAGENT_HTTP ExecutionTargetResolver."""

import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_CONFIG,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
    LocalAgentHttpExecutionTargetResolver,
)
from app.core.evaluation import ExecutionTargetRef, VersionRef


BASE_URL = "http://localagent.test"


def authoritative_ref(**changes: object) -> ExecutionTargetRef:
    values: dict[str, object] = {
        "target_id": LOCALAGENT_HTTP_TARGET_ID,
        "target_kind": LOCALAGENT_HTTP_TARGET_KIND,
        "target_version_ref": LOCALAGENT_HTTP_TARGET_VERSION,
        "config_ref": LOCALAGENT_HTTP_CONFIG,
    }
    values.update(changes)
    return ExecutionTargetRef(**values)  # type: ignore[arg-type]


def test_resolver_returns_localagent_http_target_for_valid_ref() -> None:
    resolver = LocalAgentHttpExecutionTargetResolver(base_url=BASE_URL)
    target = resolver.resolve(authoritative_ref())

    assert isinstance(target, LocalAgentHttpExecutionTarget)
    assert target.target_ref == authoritative_ref()


def test_resolved_target_ref_matches_authoritative_for_loop_validation() -> None:
    resolver = LocalAgentHttpExecutionTargetResolver(base_url=BASE_URL)
    target = resolver.resolve(authoritative_ref())

    # _validate_resolved_target in the loop requires strict equality.
    assert target.target_ref == authoritative_ref()
    assert target.target_ref.target_kind == LOCALAGENT_HTTP_TARGET_KIND
    assert target.target_ref.target_version_ref == LOCALAGENT_HTTP_TARGET_VERSION
    assert target.target_ref.config_ref == LOCALAGENT_HTTP_CONFIG


@pytest.mark.parametrize(
    "change",
    [
        {"target_kind": "FIXTURE"},
        {"target_id": "other"},
        {"target_version_ref": VersionRef("localagent_http_execution_target", "v2")},
        {"config_ref": VersionRef("localagent_http_config", "other-v1")},
    ],
    ids=["wrong-kind", "wrong-id", "wrong-version", "wrong-config"],
)
def test_resolver_fails_closed_on_unsupported_identity(change: dict[str, object]) -> None:
    resolver = LocalAgentHttpExecutionTargetResolver(base_url=BASE_URL)
    with pytest.raises(ValueError, match="unsupported"):
        resolver.resolve(authoritative_ref(**change))


def test_resolver_fails_closed_on_empty_base_url() -> None:
    resolver = LocalAgentHttpExecutionTargetResolver(base_url="")
    with pytest.raises(ValueError, match="non-empty"):
        resolver.resolve(authoritative_ref())


def test_resolver_fails_closed_on_invalid_base_url() -> None:
    resolver = LocalAgentHttpExecutionTargetResolver(base_url="ftp://localagent.test")
    with pytest.raises(ValueError, match="absolute HTTP"):
        resolver.resolve(authoritative_ref())
