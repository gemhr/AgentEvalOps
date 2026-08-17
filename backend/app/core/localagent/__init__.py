"""LocalAgent Stage3-WP4-C compatibility boundary (consumer side).

This package implements the AgentEvalOps-side strict compatibility
boundary approved by the WP4-C Architecture Decision: a versioned,
fail-closed ingestion DTO, exact contract identity/version/fingerprint
validation, frozen semantic validation, a deterministic canonical
payload digest and ownership-safe persistence.  It does NOT modify the
legacy ``POST /traces`` contract.
"""
