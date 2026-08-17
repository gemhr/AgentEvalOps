"""Real authentication integration tests for the LocalAgent compatibility endpoint.

Uses REAL persisted organizations, API keys and projects through the real
``require_localagent_project`` dependency (never overridden).  Proves the
existing-project-only M2M profile: valid/invalid/expired/revoked keys,
foreign-org projects, missing/malformed projects and NO project auto-create.
"""

# ruff: noqa: D415

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.entities import (
    AUTHENTICATION_FAILED,
    PROJECT_FORBIDDEN,
    PROJECT_NOT_FOUND,
)
from app.infrastructure.db.models import ProjectModel
from app.infrastructure.db.repositories.identity_repo import IdentityRepository
from app.infrastructure.db.repositories.project_repo import ProjectRepository
from app.registry.security import hash_api_key

from .conftest import TEST_ORG_ID, TEST_PROJECT_ID

pytestmark = pytest.mark.asyncio

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
URL = "/integrations/localagent/v1/trace-envelopes"


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-auth",
        "trace_id": "trace-auth",
        "span_id": "span-auth",
        "parent_span_id": None,
        "step_id": "step-1",
        "operation": "runtime.step",
        "component": "planner",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
        "status": "OK",
        "error_code": None,
        "attributes": {"execution_kind": "AGENT"},
    }
    body.update(overrides)
    return body


async def _mint_key(session: AsyncSession, org_id, raw_key: str, *, expires_at=None, active: bool = True) -> str:
    key = await IdentityRepository(session).create_api_key(
        org_id=org_id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:10],
        name="test-auth-key",
        expires_at=expires_at,
    )
    if not active:
        await IdentityRepository(session).revoke_api_key(key.id)
    await session.commit()
    return raw_key


async def _count_projects(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(ProjectModel))).scalar_one()


@pytest.fixture
async def auth_key(db_session: AsyncSession) -> str:
    """Valid persisted key for the seeded org/project."""
    return await _mint_key(db_session, TEST_ORG_ID, "sk_pp_test_auth_key_0000000000000001")


def headers_for(key: str, project_id: str | None) -> dict[str, str]:
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if project_id is not None:
        headers["X-Project-ID"] = project_id
    return headers


async def test_valid_key_and_project_201(client: AsyncClient, db_session: AsyncSession, auth_key: str):
    resp = await client.post(URL, json=payload(), headers=headers_for(auth_key, str(TEST_PROJECT_ID)))
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "PERSISTED"


async def test_missing_key_401(client: AsyncClient):
    resp = await client.post(URL, json=payload(), headers={"X-Project-ID": str(TEST_PROJECT_ID)})
    assert resp.status_code == 401
    assert resp.json() == {"status": "REJECTED", "error_code": AUTHENTICATION_FAILED}


async def test_missing_project_header_404(client: AsyncClient, auth_key: str):
    resp = await client.post(URL, json=payload(), headers=headers_for(auth_key, None))
    assert resp.status_code == 404
    assert resp.json() == {"status": "REJECTED", "error_code": PROJECT_NOT_FOUND}


async def test_invalid_key_401(client: AsyncClient):
    resp = await client.post(
        URL, json=payload(), headers=headers_for("sk_pp_does_not_exist", str(TEST_PROJECT_ID))
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == AUTHENTICATION_FAILED


async def test_expired_key_401(client: AsyncClient, db_session: AsyncSession):
    raw = await _mint_key(
        db_session,
        TEST_ORG_ID,
        "sk_pp_test_auth_key_0000000000000002",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    resp = await client.post(URL, json=payload(), headers=headers_for(raw, str(TEST_PROJECT_ID)))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == AUTHENTICATION_FAILED


async def test_revoked_key_401(client: AsyncClient, db_session: AsyncSession):
    raw = await _mint_key(
        db_session, TEST_ORG_ID, "sk_pp_test_auth_key_0000000000000003", active=False
    )
    resp = await client.post(URL, json=payload(), headers=headers_for(raw, str(TEST_PROJECT_ID)))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == AUTHENTICATION_FAILED


async def test_foreign_org_project_403(client: AsyncClient, db_session: AsyncSession, auth_key: str):
    other_org = await IdentityRepository(db_session).create_organization("Other Org")
    other_project = await ProjectRepository(db_session).create_project(other_org.id, "Other Project")
    await db_session.commit()

    resp = await client.post(URL, json=payload(), headers=headers_for(auth_key, str(other_project.id)))
    assert resp.status_code == 403
    assert resp.json() == {"status": "REJECTED", "error_code": PROJECT_FORBIDDEN}


async def test_missing_project_404_and_no_auto_create(client: AsyncClient, db_session: AsyncSession, auth_key: str):
    before = await _count_projects(db_session)
    resp = await client.post(URL, json=payload(), headers=headers_for(auth_key, str(uuid4())))
    assert resp.status_code == 404
    assert resp.json() == {"status": "REJECTED", "error_code": PROJECT_NOT_FOUND}
    assert await _count_projects(db_session) == before


async def test_malformed_project_id_404(client: AsyncClient, auth_key: str):
    resp = await client.post(URL, json=payload(), headers=headers_for(auth_key, "not-a-uuid"))
    assert resp.status_code == 404
    assert resp.json()["error_code"] == PROJECT_NOT_FOUND


async def test_project_name_header_is_ignored_no_auto_create(
    client: AsyncClient, db_session: AsyncSession, auth_key: str
):
    """X-Project-Name must never auto-create a project for this endpoint."""
    before = await _count_projects(db_session)
    headers = headers_for(auth_key, str(TEST_PROJECT_ID))
    headers["X-Project-Name"] = "Brand New Project That Does Not Exist"
    resp = await client.post(URL, json=payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    assert await _count_projects(db_session) == before
    existing = await ProjectRepository(db_session).get_project_by_name(TEST_ORG_ID, "Brand New Project That Does Not Exist")
    assert existing is None
