"""LocalAgent compatibility ingestion service.

Orchestrates strict validation (contract identity/version/fingerprint,
frozen envelope semantics), the deterministic canonical payload digest
and the transactional persistence behind the compatibility endpoint.
The route commits only after the service reports success, so a 2xx
always means PostgreSQL commit.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.validation import (
    canonical_payload_digest,
    validate_contract,
    validate_envelope_semantics,
)
from app.infrastructure.db.repositories.localagent_trace_repo import (
    INGEST_OUTCOME_DUPLICATE,
    INGEST_OUTCOME_PERSISTED,
    LocalAgentTraceRepository,
)

INGEST_OUTCOME_PERSISTED = INGEST_OUTCOME_PERSISTED
INGEST_OUTCOME_DUPLICATE = INGEST_OUTCOME_DUPLICATE


class LocalAgentTraceService:
    """Application use-case for one LocalAgent envelope ingestion."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with the request-scoped async session."""
        self._session = session

    async def ingest(
        self,
        envelope: LocalAgentTraceEnvelopeInV1,
        project_id: UUID,
    ) -> str:
        """Validate, digest and persist one envelope.

        Returns ``PERSISTED`` or ``DUPLICATE_ACCEPTED``.  All validation
        is fail-closed; no compatibility migration or up-conversion is
        attempted.  The caller is responsible for committing.
        """
        validate_contract(envelope)
        validate_envelope_semantics(envelope)
        digest = canonical_payload_digest(envelope)
        return await LocalAgentTraceRepository(self._session).ingest(envelope, project_id, digest)
