"""Pending-attempt scanner dispatch contract tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.infrastructure.queue import tasks
from app.services.evaluation import legacy_application


class _SessionContext:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_pending_scanner_selects_and_redispatches_only_pending_identity(monkeypatch):
    project_id, pending_attempt_id = uuid4(), uuid4()
    context = _SessionContext([(project_id, pending_attempt_id)])
    monkeypatch.setattr(legacy_application, "async_session_factory", lambda: context)
    dispatch = MagicMock()
    monkeypatch.setattr(
        tasks,
        "execute_evaluation_attempt",
        SimpleNamespace(apply_async=dispatch),
    )

    assert await legacy_application.dispatch_pending_attempts() == 1
    assert await legacy_application.dispatch_pending_attempts() == 1

    sql = str(context.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "evaluation_attempts.status = 'PENDING'" in sql
    assert "TERMINAL" not in sql
    assert "CLAIMED" not in sql
    assert "RUNNING" not in sql
    assert dispatch.call_count == 2
    assert all(
        call.kwargs["args"] == [str(project_id), str(pending_attempt_id)]
        for call in dispatch.call_args_list
    )
