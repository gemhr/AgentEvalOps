"""PostgreSQL repository for the LocalAgent compatibility boundary.

Ownership and duplicate truth is enforced by database uniqueness plus a
transactional reread/classification — never by a SELECT pre-check alone.
All identity bindings, the envelope sidecar and the legacy Trace/Span
write-model rows are written in ONE transaction; the caller commits only
after success, so a 2xx is never returned before PostgreSQL commit and a
conflict rolls everything back (zero mutation).
"""

from __future__ import annotations

import uuid
from typing import Literal
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.entities import (
    LocalAgentEnvelopeConflictError,
    LocalAgentInternalError,
    LocalAgentOwnershipConflictError,
    LocalAgentTraceEnvelopeInV1,
    LocalAgentTraceIdentityConflictError,
)
from app.core.localagent.mapper import (
    legacy_span_row,
    legacy_trace_row,
    normalized_span,
    normalized_trace,
)
from app.infrastructure.db.models import (
    LocalAgentExternalSpanIdentityModel,
    LocalAgentExternalTraceIdentityModel,
    LocalAgentTraceEnvelopeSidecarModel,
    SpanModel,
    TraceModel,
)
from app.infrastructure.db.repositories.trace_repo import TraceRepository

INGEST_OUTCOME_PERSISTED = "PERSISTED"
INGEST_OUTCOME_DUPLICATE = "DUPLICATE_ACCEPTED"

_IngestOutcome = Literal["PERSISTED", "DUPLICATE_ACCEPTED"]

_trace_identity = LocalAgentExternalTraceIdentityModel.__table__
_span_identity = LocalAgentExternalSpanIdentityModel.__table__
_sidecar = LocalAgentTraceEnvelopeSidecarModel.__table__
_traces = TraceModel.__table__
_spans = SpanModel.__table__


class LocalAgentTraceRepository:
    """Persists validated LocalAgent envelopes with immutable identity semantics."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with the request-scoped async session."""
        self._session = session

    async def _get_trace_binding(self, external_trace_id: str) -> Row[object] | None:
        """Read the immutable external trace identity binding, if any."""
        return (
            await self._session.execute(
                select(_trace_identity).where(_trace_identity.c.external_trace_id == external_trace_id)
            )
        ).one_or_none()

    async def _get_span_binding(self, external_span_id: str) -> Row[object] | None:
        """Read the immutable external span identity binding, if any."""
        return (
            await self._session.execute(
                select(_span_identity).where(_span_identity.c.external_span_id == external_span_id)
            )
        ).one_or_none()

    async def _get_sidecar_digest(self, external_span_id: str, project_id: UUID) -> str | None:
        """Read the stored canonical digest for an accepted span, if any."""
        return (
            await self._session.execute(
                select(_sidecar.c.canonical_payload_digest).where(
                    _sidecar.c.external_span_id == external_span_id,
                    _sidecar.c.project_id == project_id,
                )
            )
        ).scalar_one_or_none()

    async def ingest(
        self,
        envelope: LocalAgentTraceEnvelopeInV1,
        project_id: UUID,
        digest: str,
    ) -> _IngestOutcome:
        """Persist one validated envelope in the caller's transaction.

        Returns ``PERSISTED`` (new commit) or ``DUPLICATE_ACCEPTED``
        (exact replay, zero mutation).  Raises the typed compatibility
        errors on ownership/digest/trace-identity conflicts; the caller's
        transaction rolls back, leaving zero residual rows.
        """
        # 1. Span ownership + duplicate classification (read-only fast path).
        span_binding = await self._get_span_binding(envelope.span_id)
        if span_binding is not None:
            if span_binding.project_id != project_id:
                raise LocalAgentOwnershipConflictError()
            if span_binding.external_trace_id != envelope.trace_id:
                raise LocalAgentEnvelopeConflictError()
            existing_digest = await self._get_sidecar_digest(envelope.span_id, project_id)
            if existing_digest == digest:
                return INGEST_OUTCOME_DUPLICATE
            raise LocalAgentEnvelopeConflictError()

        # 2. Trace ownership + run identity classification (read-only).
        trace_binding = await self._get_trace_binding(envelope.trace_id)
        if trace_binding is not None:
            if trace_binding.project_id != project_id:
                raise LocalAgentOwnershipConflictError()
            if trace_binding.run_id != envelope.run_id:
                raise LocalAgentTraceIdentityConflictError()
            internal_trace_uuid = trace_binding.internal_trace_uuid
        else:
            internal_trace_uuid = uuid.uuid4()

        # 3. Parent classification (read-only): resolved only when it already
        #    belongs to this project AND this trace; otherwise a typed external
        #    reference is kept in the sidecar and no parent row is fabricated.
        internal_parent_uuid: UUID | None = None
        if envelope.parent_span_id is not None:
            parent_binding = await self._get_span_binding(envelope.parent_span_id)
            if parent_binding is not None:
                if parent_binding.project_id != project_id:
                    raise LocalAgentOwnershipConflictError()
                if parent_binding.external_trace_id != envelope.trace_id:
                    raise LocalAgentEnvelopeConflictError()
                internal_parent_uuid = parent_binding.internal_span_uuid

        # 4. Trace identity binding insert — DB uniqueness arbitrates races.
        trace_insert = await self._session.execute(
            pg_insert(_trace_identity)
            .values(
                external_trace_id=envelope.trace_id,
                project_id=project_id,
                internal_trace_uuid=internal_trace_uuid,
                run_id=envelope.run_id,
            )
            .on_conflict_do_nothing(index_elements=["external_trace_id"])
        )
        if trace_insert.rowcount == 0:  # type: ignore[union-attr]
            trace_binding = await self._get_trace_binding(envelope.trace_id)
            if trace_binding is None:
                raise LocalAgentInternalError()
            if trace_binding.project_id != project_id:
                raise LocalAgentOwnershipConflictError()
            if trace_binding.run_id != envelope.run_id:
                raise LocalAgentTraceIdentityConflictError()
            internal_trace_uuid = trace_binding.internal_trace_uuid

        # 5. Span identity binding insert — DB uniqueness arbitrates races.
        internal_span_uuid = uuid.uuid4()
        span_insert = await self._session.execute(
            pg_insert(_span_identity)
            .values(
                external_span_id=envelope.span_id,
                project_id=project_id,
                internal_span_uuid=internal_span_uuid,
                external_trace_id=envelope.trace_id,
            )
            .on_conflict_do_nothing(index_elements=["external_span_id"])
        )
        if span_insert.rowcount == 0:  # type: ignore[union-attr]
            span_binding = await self._get_span_binding(envelope.span_id)
            if span_binding is None:
                raise LocalAgentInternalError()
            if span_binding.project_id != project_id:
                raise LocalAgentOwnershipConflictError()
            if span_binding.external_trace_id != envelope.trace_id:
                raise LocalAgentEnvelopeConflictError()
            existing_digest = await self._get_sidecar_digest(envelope.span_id, project_id)
            if existing_digest == digest:
                return INGEST_OUTCOME_DUPLICATE
            raise LocalAgentEnvelopeConflictError()

        # 6. Legacy write model + immutable sidecar in the same transaction.
        await self._session.execute(
            pg_insert(_traces)
            .values(**legacy_trace_row(envelope, internal_trace_uuid=internal_trace_uuid, project_id=project_id))
            .on_conflict_do_nothing(index_elements=["trace_id"])
        )
        await self._session.execute(
            pg_insert(_spans).values(
                **legacy_span_row(
                    envelope,
                    internal_span_uuid=internal_span_uuid,
                    internal_trace_uuid=internal_trace_uuid,
                    internal_parent_uuid=internal_parent_uuid,
                )
            )
        )
        await self._session.execute(
            pg_insert(_sidecar).values(
                envelope_id=uuid.uuid4(),
                project_id=project_id,
                external_run_id=envelope.run_id,
                external_trace_id=envelope.trace_id,
                external_span_id=envelope.span_id,
                external_parent_span_id=envelope.parent_span_id,
                step_id=envelope.step_id,
                operation=envelope.operation,
                component=envelope.component,
                started_at=envelope.started_at,
                completed_at=envelope.completed_at,
                # Lossless authoritative storage: the semantic Decimal is written
                # into the PostgreSQL NUMERIC column as-is.  No float conversion
                # (P1-06) — ``2**53+1`` and the 309-digit MAX are preserved
                # exactly and read back equal from a fresh session.
                duration_ms=envelope.duration_ms,
                status=envelope.status,
                error_code=envelope.error_code,
                attributes=dict(envelope.attributes),
                contract_identity=envelope.contract_identity,
                contract_version=envelope.contract_version,
                contract_fingerprint=envelope.contract_fingerprint,
                canonical_payload_digest=digest,
                internal_trace_uuid=internal_trace_uuid,
                internal_span_uuid=internal_span_uuid,
            )
        )
        await TraceRepository(self._session).persist_normalized(
            normalized_trace(
                envelope,
                internal_trace_uuid=internal_trace_uuid,
                project_id=project_id,
            ),
            normalized_span(
                envelope,
                internal_trace_uuid=internal_trace_uuid,
                internal_span_uuid=internal_span_uuid,
                internal_parent_uuid=internal_parent_uuid,
                project_id=project_id,
            ),
        )
        return INGEST_OUTCOME_PERSISTED
