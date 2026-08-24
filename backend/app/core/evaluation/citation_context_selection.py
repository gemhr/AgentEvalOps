"""WP5 evaluation-only Citation Context Selection domain (fixed-top-k.v1).

This module implements the evaluation-side, deterministic context-selection
experiment on top of the frozen WP4 RRF ranked evidence and a frozen controlled
corpus. It owns only ``DERIVED_CONTEXT_SELECTION``: it never re-runs retrieval,
never re-computes RRF rank/score, never invokes LocalAgent runtime, and never
writes back to ``RagEvaluationArtifactV1`` or Runtime status.

Frozen WP5 v1 scope:

- ``policy_ref = fixed-top-k.v1``, ``K in {1, 2, 3, 4}`` (single free variable).
- Selection = the first K valid, materializable candidates of the frozen RRF
  ranked list, ordered by frozen RRF rank ascending.
- No score threshold, token-budget optimization, source diversity, dynamic K,
  support-aware selection, or query-conditioned rule.
- ``serializer_ref = citation-context-serializer.v1`` (evaluation-side fixed
  serialization of selected RAG blocks, consistent with the production
  rendered-context source/citation envelope).
- Token counts come from the pinned generation-model tokenizer, never from
  ``DeterministicTokenEstimator``.
- Citation Correctness / Completeness are ``NOT_EVALUATED_IN_WP5_V1``.

GroundTruth boundary: the selector is label-blind. It only reads candidate
identities, ranks, and materialized content digests. Ground truth
(``expected_support_fact_ids``, answerability, case type) is consumed only
*after* selection to compute metrics, and only for eligible cases.
"""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator, model_validator

CITATION_CONTEXT_SELECTION_SCHEMA_VERSION = "citation-context-selection.v1"
CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION = "citation-context-comparison.v1"
SERIALIZER_REF = "citation-context-serializer.v1"
SERIALIZER_VERSION = "v1"
POLICY_REF = "fixed-top-k.v1"
K_VALUES = (1, 2, 3, 4)
MAX_K = 4
RUNTIME_CONTEXT_BASELINE = "localagent-retrieval-context.v1"
WP5_COMPARISON_BASELINE = "fixed-top-k.v1/K=4"

# Frozen generation-model / tokenizer identity (external Authority).
GENERATION_MODEL_REF = "qwen2.5-7b-instruct-q4_k_m.gguf"
GENERATION_TOKENIZER_REF = "qwen2.5-7b-instruct-q4_k_m.gguf"
EXPECTED_GGUF_SHA256 = "f9988096ab4497d9c4a624c1a1da6de888d7622a3979b7e03d95385c927d5e05"
LLAMA_CPP_VERSION = "0.2.90"

# Explicitly frozen tokenization mode (llama-cpp 0.2.90 signature audited fresh:
# ``Llama.tokenize(text: bytes, add_bos: bool = True, special: bool = False)``).
# WP5 counts the serialized RAG context text's own token cost; BOS is a whole
# sequence-start token not attributable to this RAG segment, so add_bos=False.
TOKENIZATION_MODE_REF = "llama-cpp-tokenize.v1"
TOKENIZE_ADD_BOS = False
TOKENIZE_SPECIAL = False
TOKEN_USAGE_AUTHORITY = "PINNED_GENERATION_MODEL_TOKENIZER_ON_WP5_SERIALIZED_RAG_CONTEXT"

# Truthful evaluation-side dedup labeling (NOT production-exact).
WP5_DEDUP_REF = "evaluation-raw-snippet-sha256-dedup.v1"
WP5_DEDUP_IS_PRODUCTION_EXACT = False

# Frozen controlled-corpus manifest identities (external source/chunk Authority).
EXPECTED_SOURCE_MANIFEST_DIGEST = "4da8c504a8ad77ae6c8dd9ec004c7178f26fe5ee7be1a4cf94b822bce9b427f6"
EXPECTED_CHUNK_MANIFEST_DIGEST = "149a39a7d6b45fb7484f934288037f787b6322dd13d135fd721b4a1d5117cc91"

DROP_RANK_AFTER_K = "rank_after_k"
DROP_DUPLICATE_CONTENT = "duplicate_content"
DROP_MATERIALIZATION_INVALID = "materialization_invalid"
DROP_REASONS = (DROP_RANK_AFTER_K, DROP_DUPLICATE_CONTENT, DROP_MATERIALIZATION_INVALID)

NOT_EVALUATED_IN_WP5_V1 = "NOT_EVALUATED_IN_WP5_V1"
NOT_RUN = "NOT_RUN"


class CitationContextSelectionError(ValueError):
    """WP5 selection domain protocol error."""


class CitationContextMaterializationError(CitationContextSelectionError):
    """A ranked candidate could not be materialized against the frozen corpus."""


class CitationContextProtocolError(CitationContextSelectionError):
    """A required denominator or selection invariant is violated."""


def canonical_digest(value: object) -> str:
    """Stable canonical-JSON SHA-256 (matches WP4 service helper)."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("value must be a lowercase SHA-256 hex digest")
    return value


def _sha1(value: str) -> str:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("value must be a lowercase SHA-1 hex digest")
    return value


_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")


def _require_wire_id(value: str, field_name: str) -> str:
    if not _WIRE_ID.match(value):
        raise ValueError(f"invalid {field_name} format: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Controlled corpus (frozen chunk-manifest identity + plaintext materialization)
# ---------------------------------------------------------------------------


class ControlledCorpusEntry(BaseModel):
    """A materialized frozen-corpus chunk (plaintext held only in memory)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    source: StrictStr
    section_path: str | None = None
    content_hash: StrictStr  # frozen chunk manifest SHA-1 of the raw snippet
    content_digest: StrictStr  # canonical SHA-256 of the snippet (sidecar identity)
    snippet: StrictStr

    @field_validator("document_id", "chunk_id", "source")
    @classmethod
    def _ids(cls, value: str, info: object) -> str:
        return _require_wire_id(value, getattr(info, "field_name", "corpus_identity"))

    @field_validator("section_path")
    @classmethod
    def _section(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or "\\" in value or value.startswith(("/", "file:")):
            raise ValueError("section_path must be a safe non-path label")
        return value

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        return _sha1(value)

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def _digests(self) -> "ControlledCorpusEntry":
        if hashlib.sha1(self.snippet.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("content_hash must match SHA-1 of snippet")
        if hashlib.sha256(self.snippet.encode("utf-8")).hexdigest() != self.content_digest:
            raise ValueError("content_digest must match SHA-256 of snippet")
        return self


class ControlledCorpus(BaseModel):
    """Frozen controlled corpus: chunk manifest identity + plaintext lookup.

    ``entries`` is keyed by ``chunk_id``. ``chunk_manifest_digest`` /
    ``source_manifest_digest`` bind the frozen substrate; loading verifies that
    the ordered entries reproduce the frozen chunk manifest digest exactly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_ref: Literal["rag-evaluation-corpus.v1"]
    source_manifest_digest: StrictStr
    chunk_manifest_digest: StrictStr
    entries: tuple[ControlledCorpusEntry, ...]

    @field_validator("source_manifest_digest", "chunk_manifest_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def _unique_entries(self) -> "ControlledCorpus":
        ids = [entry.chunk_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate controlled corpus chunk_id is not allowed")
        return self

    @classmethod
    def from_entries(
        cls,
        entries: Sequence[ControlledCorpusEntry],
        *,
        corpus_ref: str,
        source_manifest_digest: str,
        chunk_manifest_digest: str,
    ) -> "ControlledCorpus":
        ordered = tuple(entries)
        reproduced = ordered_chunk_manifest_digest(ordered)
        if reproduced != chunk_manifest_digest:
            raise CitationContextMaterializationError(
                "controlled corpus chunk manifest digest does not reproduce frozen manifest"
            )
        return cls(
            corpus_ref=corpus_ref,
            source_manifest_digest=source_manifest_digest,
            chunk_manifest_digest=chunk_manifest_digest,
            entries=ordered,
        )

    def _by_chunk(self) -> Mapping[str, ControlledCorpusEntry]:
        return {entry.chunk_id: entry for entry in self.entries}

    def materialize(self, *, document_id: str, chunk_id: str) -> ControlledCorpusEntry:
        """Resolve a frozen candidate identity to corpus plaintext, fail closed."""
        entry = self._by_chunk().get(chunk_id)
        if entry is None:
            raise CitationContextMaterializationError(f"candidate chunk missing from controlled corpus: {chunk_id}")
        if entry.document_id != document_id:
            raise CitationContextMaterializationError("candidate document_id does not match corpus entry")
        return entry


def ordered_chunk_manifest_digest(entries: Sequence[ControlledCorpusEntry]) -> str:
    """Reproduce the frozen ordered chunk-manifest digest (identity-authority).

    Mirrors LocalAgent ``evaluation_environment.ordered_chunk_manifest_digest``:
    canonical compact JSON over the deterministic ordered identity dicts.
    """
    identities = [
        {
            "document_id": entry.document_id,
            "chunk_id": entry.chunk_id,
            "source": entry.source,
            "section_path": entry.section_path or "",
            "content_hash": entry.content_hash,
        }
        for entry in entries
    ]
    canonical = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CitationContextTokenizerError(CitationContextSelectionError):
    """Frozen tokenizer identity/mode is not satisfiable."""


class TokenizerIdentityMismatch(CitationContextTokenizerError):
    """The supplied generation-model file does not match the frozen GGUF identity."""


def source_manifest_digest(files: Sequence[object]) -> str:
    """Reproduce the frozen canonical source-manifest digest (external Authority).

    Mirrors LocalAgent ``evaluation_environment.source_manifest_digest``:
    stable-sort by relative source path, canonical compact JSON over
    ``[{"path": ..., "sha256": ...}, ...]``. Accepts dicts or ``SourceFile``.
    """
    entries = [
        {"path": item["path"] if isinstance(item, Mapping) else item.path,
         "sha256": item["sha256"] if isinstance(item, Mapping) else item.sha256}
        for item in files
    ]
    entries = sorted(entries, key=lambda entry: entry["path"])
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr
    sha256: StrictStr

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.strip() or "\\" in value or value.startswith(("/", "file:")):
            raise ValueError("source path must be a safe relative posix path")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)


class SourceManifest(BaseModel):
    """Independent frozen source authority (privacy-safe: paths + sha256 only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["wp5-source-manifest.v1"]
    corpus_ref: Literal["rag-evaluation-corpus.v1"]
    source_manifest_digest: StrictStr
    files: tuple[SourceFile, ...]

    @field_validator("source_manifest_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def _self_consistent(self) -> "SourceManifest":
        if source_manifest_digest(self.files) != self.source_manifest_digest:
            raise CitationContextMaterializationError(
                "source manifest declared digest does not reproduce from its files"
            )
        paths = [f.path for f in self.files]
        if len(paths) != len(set(paths)):
            raise CitationContextMaterializationError("duplicate source path is not allowed")
        return self


def validate_external_source_authority(
    manifest: SourceManifest,
    *,
    expected: str = EXPECTED_SOURCE_MANIFEST_DIGEST,
) -> str:
    """Fail closed unless the independent source manifest binds the frozen source digest."""
    if manifest.source_manifest_digest != expected:
        raise CitationContextMaterializationError(
            "independent source manifest does not match frozen source-manifest authority"
        )
    return manifest.source_manifest_digest


def verify_materialized_sources(
    entries: Sequence[ControlledCorpusEntry],
    manifest_files: Sequence[SourceFile],
) -> None:
    """Bind materialized corpus source set to the independent source manifest.

    Rejects wrong / missing / extra source filename (identity drift).
    """
    materialized = {entry.source for entry in entries}
    expected = {file.path for file in manifest_files}
    if materialized != expected:
        raise CitationContextMaterializationError(
            "materialized corpus source set does not match independent source manifest"
        )


# ---------------------------------------------------------------------------
# Internal (memory-only) candidate / selection views
# ---------------------------------------------------------------------------


class CandidateView(BaseModel):
    """A materialized ranked candidate held in memory (never written to sidecar)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1, le=8)
    content_hash: StrictStr
    content_digest: StrictStr
    snippet: StrictStr
    source: StrictStr

    @field_validator("query_sha256")
    @classmethod
    def _query(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha1(value)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)


class CaseCandidateView(BaseModel):
    """All materialized ranked candidates of one case (label-blind population)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    candidates: tuple[CandidateView, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _ranks(self) -> "CaseCandidateView":
        ranks = tuple(candidate.rank for candidate in self.candidates)
        if ranks != tuple(range(1, len(self.candidates) + 1)):
            raise CitationContextSelectionError("candidate ranks must be exact 1..N")
        identities = [(c.document_id, c.chunk_id) for c in self.candidates]
        if len(identities) != len(set(identities)):
            raise CitationContextSelectionError("duplicate candidate identity is not allowed")
        return self


class DroppedView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1)
    reason: Literal[DROP_RANK_AFTER_K, DROP_DUPLICATE_CONTENT, DROP_MATERIALIZATION_INVALID]


class SelectedView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    original_rank: int = Field(ge=1)
    selected_order: int = Field(ge=1)
    content_digest: StrictStr
    serialized_token_count: int = Field(ge=0)
    block: StrictStr  # serialized block (memory only)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)


class CaseSelectionResult(BaseModel):
    """Label-blind selection outcome for one case (memory-only, holds plaintext)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    eligible: bool
    candidates: tuple[SelectedView | DroppedView, ...] = Field(min_length=1)
    selected: tuple[SelectedView, ...]
    dropped: tuple[DroppedView, ...]
    selected_expected_support_ids: tuple[str, ...] = ()
    full_serialized_token_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _coverage(self) -> "CaseSelectionResult":
        combined = list(self.selected) + list(self.dropped)
        if len(combined) != len(self.candidates):
            raise CitationContextSelectionError("selection candidates do not match input count")
        return self


# ---------------------------------------------------------------------------
# Fixed serializer (citation-context-serializer.v1)
# ---------------------------------------------------------------------------


class CitationContextSerializer:
    r"""Evaluation-side fixed serialization of selected RAG context blocks.

    Envelope mirrors the production rendered-context RAG_DOCUMENT format:
    ``[来源: {source}]\n{content}[引用: {citation_id}]``. Blocks are joined by a
    single blank line. It serializes only the selected RAG blocks; it never
    includes system/user prompts or reserved output tokens.
    """

    ref: str = SERIALIZER_REF
    version: str = SERIALIZER_VERSION

    @staticmethod
    def citation_id(selected_order: int) -> str:
        return f"C{selected_order}"

    @staticmethod
    def serialize_block(*, source: str, content: str, selected_order: int) -> str:
        return f"[来源: {source}]\n{content}[引用: {CitationContextSerializer.citation_id(selected_order)}]"

    @staticmethod
    def serialize_context(blocks: Sequence[str]) -> str:
        return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Pinned tokenizer (real generation-model tokenizer, offline + local-only)
# ---------------------------------------------------------------------------


class TokenCounter(Protocol):
    """Real offline tokenizer used as WP5 token-count authority."""

    tokenizer_ref: str
    tokenizer_identity: str
    tokenization_mode_ref: str
    add_bos: bool
    special: bool
    tokenizer_authority: Literal["PINNED_GENERATION_MODEL", "FIXTURE_TEST_ONLY"]

    def count(self, text: str) -> int: ...


def verify_tokenizer_file(model_path: str) -> None:
    """Fail closed unless the GGUF bytes and filename match the frozen identity.

    Authority is the file bytes (fresh SHA-256) and the frozen reference name;
    the caller cannot self-report the identity.
    """
    path = Path(model_path)
    if path.name != GENERATION_MODEL_REF:
        raise TokenizerIdentityMismatch(
            f"generation model filename does not match frozen ref: {path.name!r}"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_GGUF_SHA256:
        raise TokenizerIdentityMismatch(
            "generation model bytes do not match frozen EXPECTED_GGUF_SHA256"
        )


class LlamaCppTokenCounter:
    """Real generation-model tokenizer via llama-cpp (offline, deterministic).

    Loads the frozen GGUF generation model in local-only mode after verifying the
    file identity (name + fresh SHA-256) against the frozen constants. ``llama_cpp``
    is imported lazily so that ``--help`` and pure domain paths never load it.
    Tokenization uses the explicitly frozen ``add_bos`` / ``special`` mode so it
    cannot drift with llama-cpp defaults.
    """

    def __init__(self, model_path: str) -> None:
        verify_tokenizer_file(model_path)
        import llama_cpp

        self._llm = llama_cpp.Llama(model_path=model_path, n_ctx=4096, n_threads=4, n_gpu_layers=0, verbose=False)
        self.tokenizer_ref = GENERATION_TOKENIZER_REF
        self.tokenizer_identity = EXPECTED_GGUF_SHA256
        self.tokenization_mode_ref = TOKENIZATION_MODE_REF
        self.add_bos = TOKENIZE_ADD_BOS
        self.special = TOKENIZE_SPECIAL
        self.tokenizer_authority = "PINNED_GENERATION_MODEL"

    def count(self, text: str) -> int:
        return len(self._llm.tokenize(text.encode("utf-8"), add_bos=self.add_bos, special=self.special))


FIXTURE_TOKENIZER_REF = "wp5-test-fixture-tokenizer.v1"
FIXTURE_TOKENIZER_IDENTITY = "1111111111111111111111111111111111111111111111111111111111111111"
FIXTURE_TOKENIZATION_MODE_REF = "wp5-fixture-tokenize.v1"


class FixtureTokenCounter:
    r"""Deterministic offline test-fixture tokenizer (NOT the production model).

    Used only for WP5 unit/integration tests. It is intentionally distinct from
    the production ``DeterministicTokenEstimator`` regex (which also counts every
    punctuation char via a ``\\S`` catch-all); this fixture counts only CJK
    characters and alphanumeric word tokens. Its ref/identity/mode are distinct
    from the frozen generation-model tokenizer so it can never masquerade as the
    real WP5 token-count authority. It must never be used in a real run.
    """

    def __init__(
        self,
        *,
        tokenizer_ref: str = FIXTURE_TOKENIZER_REF,
        tokenizer_identity: str = FIXTURE_TOKENIZER_IDENTITY,
    ) -> None:
        self.tokenizer_ref = tokenizer_ref
        self.tokenizer_identity = tokenizer_identity
        self.tokenization_mode_ref = FIXTURE_TOKENIZATION_MODE_REF
        self.add_bos = False
        self.special = False
        self.tokenizer_authority = "FIXTURE_TEST_ONLY"

    def count(self, text: str) -> int:
        return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+", text))


# ---------------------------------------------------------------------------
# Fixed-top-k selector (label-blind)
# ---------------------------------------------------------------------------


class FixedTopKSelector:
    """Frozen ``fixed-top-k.v1`` selection policy.

    Dedup semantics (frozen, shared by every K): candidates whose raw-snippet
    SHA-256 content digest is identical are deduplicated; the first by frozen RRF
    rank is kept and later duplicates are dropped as ``duplicate_content``.

    This is an evaluation-side deterministic raw-snippet identity dedup
    (``WP5_DEDUP_REF = evaluation-raw-snippet-sha256-dedup.v1``). It is NOT
    production-exact (``WP5_DEDUP_IS_PRODUCTION_EXACT = false``): production
    ``RetrievalExecutionService._build_context_chunks()`` dedups on a
    whitespace-normalized content hash, which WP5 does not reproduce. Selection
    is label-blind and never consults ground truth.
    """

    policy_ref: str = POLICY_REF
    dedup_ref: str = WP5_DEDUP_REF

    def select(self, candidates: Sequence[CandidateView], *, K: int) -> CaseSelectionResult:
        if K not in K_VALUES:
            raise CitationContextSelectionError("K must be one of 1, 2, 3, 4")
        try:
            case = CaseCandidateView(
                case_id=candidates[0].case_id,
                query_sha256=candidates[0].query_sha256,
                candidates=tuple(candidates),
            )
        except ValidationError as exc:
            raise CitationContextSelectionError("invalid candidate population") from exc
        ordered = sorted(case.candidates, key=lambda c: c.rank)
        if tuple(c.rank for c in ordered) != tuple(range(1, len(ordered) + 1)):
            raise CitationContextSelectionError("rank gap in candidates is not allowed")

        kept: list[CandidateView] = []
        seen_digests: set[str] = set()
        dropped: list[DroppedView] = []
        for candidate in ordered:
            if candidate.content_digest in seen_digests:
                dropped.append(
                    DroppedView(
                        document_id=candidate.document_id,
                        chunk_id=candidate.chunk_id,
                        rank=candidate.rank,
                        reason=DROP_DUPLICATE_CONTENT,
                    )
                )
                continue
            seen_digests.add(candidate.content_digest)
            kept.append(candidate)

        selected: list[SelectedView] = []
        remaining_dropped = list(dropped)
        for index, candidate in enumerate(kept[:K], start=1):
            selected.append(
                SelectedView(
                    document_id=candidate.document_id,
                    chunk_id=candidate.chunk_id,
                    original_rank=candidate.rank,
                    selected_order=index,
                    content_digest=candidate.content_digest,
                    serialized_token_count=0,
                    block=CitationContextSerializer.serialize_block(
                        source=candidate.source,
                        content=candidate.snippet,
                        selected_order=index,
                    ),
                )
            )
        for candidate in kept[K:]:
            remaining_dropped.append(
                DroppedView(
                    document_id=candidate.document_id,
                    chunk_id=candidate.chunk_id,
                    rank=candidate.rank,
                    reason=DROP_RANK_AFTER_K,
                )
            )

        combined: list[SelectedView | DroppedView] = [*selected, *remaining_dropped]
        combined_sorted = sorted(combined, key=lambda x: x.original_rank if isinstance(x, SelectedView) else x.rank)
        return CaseSelectionResult(
            case_id=case.case_id,
            query_sha256=case.query_sha256,
            eligible=True,
            candidates=tuple(combined_sorted),
            selected=tuple(selected),
            dropped=tuple(remaining_dropped),
        )


# ---------------------------------------------------------------------------
# Token counting over serialized selection
# ---------------------------------------------------------------------------


def count_selection_tokens(
    selection: CaseSelectionResult,
    token_counter: TokenCounter,
) -> CaseSelectionResult:
    """Tokenize each selected serialized block with the pinned tokenizer.

    The full serialized selected context is tokenized once as the total-count
    authority (a shared separator/header is never assumed additive).
    """
    selected = list(selection.selected)
    full_context = CitationContextSerializer.serialize_context([s.block for s in selected])
    full_count = token_counter.count(full_context)
    per_chunk: list[SelectedView] = []
    for index, item in enumerate(selected, start=1):
        block_count = token_counter.count(item.block)
        per_chunk.append(
            SelectedView(
                document_id=item.document_id,
                chunk_id=item.chunk_id,
                original_rank=item.original_rank,
                selected_order=index,
                content_digest=item.content_digest,
                serialized_token_count=block_count,
                block=item.block,
            )
        )
    return CaseSelectionResult(
        case_id=selection.case_id,
        query_sha256=selection.query_sha256,
        eligible=selection.eligible,
        candidates=selection.candidates,
        selected=tuple(per_chunk),
        dropped=selection.dropped,
        selected_expected_support_ids=selection.selected_expected_support_ids,
        full_serialized_token_count=full_count,
    )


# ---------------------------------------------------------------------------
# Metric evaluators (consume selection only; never re-select)
# ---------------------------------------------------------------------------


class CitationContextCaseMetrics(BaseModel):
    """Per-case WP5 v1 metrics (privacy-safe, numeric only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    selected_chunk_count: int = Field(ge=0)
    selected_serialized_token_count: int = Field(ge=0)
    selected_support_chunk_count: int = Field(ge=0)
    selected_non_support_chunk_count: int = Field(ge=0)
    selected_non_support_serialized_token_count: int = Field(ge=0)
    support_coverage: float | None = None
    noise_by_chunk: float | None = None
    noise_by_token: float | None = None


class CitationContextCaseMetricsBuilder:
    """Builds privacy-safe per-case metrics from a labeled selection."""

    @staticmethod
    def build(
        selection: CaseSelectionResult,
        *,
        expected_support_ids: tuple[str, ...] | None,
    ) -> CitationContextCaseMetrics:
        eligible = selection.eligible and expected_support_ids is not None
        selected = selection.selected
        selected_chunk_count = len(selected)
        selected_ids = {s.chunk_id for s in selected}
        support_ids = set(expected_support_ids or ())
        selected_support_chunk_count = len(selected_ids & support_ids)
        selected_non_support_chunk_count = selected_chunk_count - selected_support_chunk_count
        selected_non_support_tokens = sum(
            s.serialized_token_count for s in selected if s.chunk_id not in support_ids
        )
        # Total token-count authority: the full serialized selected context
        # tokenized once (includes any frozen inter-block separators/headers).
        selected_serialized_token_count = selection.full_serialized_token_count

        if not eligible:
            return CitationContextCaseMetrics(
                eligible=False,
                selected_chunk_count=selected_chunk_count,
                selected_serialized_token_count=selected_serialized_token_count,
                selected_support_chunk_count=0,
                selected_non_support_chunk_count=0,
                selected_non_support_serialized_token_count=0,
            )
        if selected_chunk_count == 0:
            raise CitationContextProtocolError("eligible case with zero selected chunks is a protocol failure")
        if selected_serialized_token_count == 0:
            raise CitationContextProtocolError("eligible case with zero serialized tokens is a protocol failure")
        if not expected_support_ids:
            raise CitationContextProtocolError("eligible case requires non-empty expected support ids")
        return CitationContextCaseMetrics(
            eligible=True,
            selected_chunk_count=selected_chunk_count,
            selected_serialized_token_count=selected_serialized_token_count,
            selected_support_chunk_count=selected_support_chunk_count,
            selected_non_support_chunk_count=selected_non_support_chunk_count,
            selected_non_support_serialized_token_count=selected_non_support_tokens,
            support_coverage=selected_support_chunk_count / len(expected_support_ids),
            noise_by_chunk=selected_non_support_chunk_count / selected_chunk_count,
            noise_by_token=selected_non_support_tokens / selected_serialized_token_count,
        )


class CitationContextAggregateMetrics(BaseModel):
    """WP5 v1 aggregate metrics (micro noise, macro coverage; documented)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible_case_count: int = Field(ge=0)
    support_coverage_macro: float | None = None
    noise_by_chunk_micro: float | None = None
    noise_by_token_micro: float | None = None
    total_serialized_context_tokens: int = Field(ge=0)
    avg_serialized_context_tokens_per_eligible_case: float | None = None


def aggregate_case_metrics(cases: Sequence[CitationContextCaseMetrics]) -> CitationContextAggregateMetrics:
    """Compute frozen aggregates over eligible cases.

    - Noise uses micro aggregation (frozen in the scope decision): sum of
      non-support numerators over sum of selected denominators.
    - Support coverage is reported as macro-average of per-case coverage (the
      scope decision does not freeze micro/macro for coverage; macro is chosen
      and documented in the handoff).
    - Token usage aggregates the full serialized context tokens.
    """
    eligible = [c for c in cases if c.eligible]
    if not eligible:
        return CitationContextAggregateMetrics(
            eligible_case_count=0, total_serialized_context_tokens=0
        )

    total_selected_chunks = sum(c.selected_chunk_count for c in eligible)
    total_non_support_chunks = sum(c.selected_non_support_chunk_count for c in eligible)
    total_tokens = sum(c.selected_serialized_token_count for c in eligible)
    total_non_support_tokens = sum(c.selected_non_support_serialized_token_count for c in eligible)

    if total_selected_chunks == 0:
        raise CitationContextProtocolError("micro noise-by-chunk denominator is zero")
    if total_tokens == 0:
        raise CitationContextProtocolError("micro noise-by-token denominator is zero")

    coverage_values = [c.support_coverage for c in eligible if c.support_coverage is not None]
    coverage_macro = sum(coverage_values) / len(coverage_values) if coverage_values else None

    return CitationContextAggregateMetrics(
        eligible_case_count=len(eligible),
        support_coverage_macro=coverage_macro,
        noise_by_chunk_micro=total_non_support_chunks / total_selected_chunks,
        noise_by_token_micro=total_non_support_tokens / total_tokens,
        total_serialized_context_tokens=total_tokens,
        avg_serialized_context_tokens_per_eligible_case=total_tokens / len(eligible),
    )


# ---------------------------------------------------------------------------
# Strict sidecar DTOs (privacy-safe; plaintext never persisted)
# ---------------------------------------------------------------------------


class CitationContextCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1, le=8)
    content_digest: StrictStr

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)


class CitationContextSelected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    original_rank: int = Field(ge=1)
    selected_order: int = Field(ge=1)
    content_digest: StrictStr
    serialized_token_count: int = Field(ge=0)

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value)


class CitationContextDropped(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1)
    reason: Literal[DROP_RANK_AFTER_K, DROP_DUPLICATE_CONTENT, DROP_MATERIALIZATION_INVALID]


class CitationContextCaseSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    eligible: bool
    candidates: tuple[CitationContextCandidate, ...] = Field(min_length=1)
    selected: tuple[CitationContextSelected, ...]
    dropped: tuple[CitationContextDropped, ...]
    selected_expected_support_ids: tuple[str, ...]
    metrics: CitationContextCaseMetrics

    @field_validator("query_sha256")
    @classmethod
    def _query(cls, value: str) -> str:
        return _sha256(value)


class CitationContextSelectionEnvelope(BaseModel):
    """Strict versioned WP5 selection sidecar (``citation-context-selection.v1``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["citation-context-selection.v1"] = CITATION_CONTEXT_SELECTION_SCHEMA_VERSION
    selection_id: StrictStr
    dataset_id: StrictStr
    dataset_version: StrictStr
    dataset_digest: StrictStr
    retrieval_evidence_schema: Literal["no-answer-rrf-evidence.v2"]
    retrieval_evidence_digest: StrictStr
    substrate_ref: Literal["wp4-no-answer-rrf-substrate.v2"]
    corpus_ref: Literal["rag-evaluation-corpus.v1"]
    source_manifest_digest: StrictStr
    chunk_manifest_digest: StrictStr
    policy_ref: Literal["fixed-top-k.v1"]
    K: int = Field(ge=1, le=4)
    serializer_ref: Literal["citation-context-serializer.v1"]
    tokenizer_ref: StrictStr
    tokenizer_identity: StrictStr
    tokenization_mode_ref: StrictStr
    add_bos: bool
    special: bool
    tokenizer_authority: Literal["PINNED_GENERATION_MODEL", "FIXTURE_TEST_ONLY"]
    runtime_read_only: Literal[True]
    cases: tuple[CitationContextCaseSidecar, ...]

    @field_validator("dataset_digest", "retrieval_evidence_digest", "source_manifest_digest", "chunk_manifest_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("tokenizer_identity")
    @classmethod
    def _tokenizer_identity(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def _tokenizer_freeze(self) -> "CitationContextSelectionEnvelope":
        """Fail closed unless the tokenizer binds the frozen real identity/mode.

        A PINNED_GENERATION_MODEL sidecar must carry the exact frozen ref, GGUF
        SHA-256, tokenization mode ref, and add_bos/special values. A fixture
        sidecar must use distinct fixture identity and must never masquerade as
        the frozen generation-model tokenizer.
        """
        if self.tokenizer_authority == "PINNED_GENERATION_MODEL":
            if self.tokenizer_ref != GENERATION_TOKENIZER_REF:
                raise CitationContextSelectionError("pinned tokenizer ref must match frozen generation model ref")
            if self.tokenizer_identity != EXPECTED_GGUF_SHA256:
                raise CitationContextSelectionError("pinned tokenizer identity must match frozen GGUF SHA-256")
            if self.tokenization_mode_ref != TOKENIZATION_MODE_REF:
                raise CitationContextSelectionError("pinned tokenization mode ref mismatch")
            if self.add_bos != TOKENIZE_ADD_BOS or self.special != TOKENIZE_SPECIAL:
                raise CitationContextSelectionError("pinned tokenization add_bos/special mismatch")
        else:  # FIXTURE_TEST_ONLY
            if self.tokenizer_ref == GENERATION_TOKENIZER_REF:
                raise CitationContextSelectionError("fixture tokenizer must not masquerade as the generation model")
            if self.tokenizer_identity == EXPECTED_GGUF_SHA256:
                raise CitationContextSelectionError("fixture tokenizer must not masquerade as the frozen GGUF")
        return self

    @model_validator(mode="after")
    def _cross_case_invariants(self) -> "CitationContextSelectionEnvelope":
        """Fail closed on selected/dropped/candidate/order/K/eligibility inconsistencies."""
        for case in self.cases:
            cand_ids = [(c.document_id, c.chunk_id) for c in case.candidates]
            if len(cand_ids) != len(set(cand_ids)):
                raise CitationContextSelectionError("duplicate candidate identity in sidecar case")
            cand_set = set(cand_ids)
            if len(case.selected) > self.K:
                raise CitationContextSelectionError("selected count exceeds K")
            sel_ids: set[tuple[str, str]] = set()
            for index, item in enumerate(case.selected, start=1):
                identity = (item.document_id, item.chunk_id)
                if identity not in cand_set:
                    raise CitationContextSelectionError("selected candidate not in candidate list")
                if identity in sel_ids:
                    raise CitationContextSelectionError("duplicate selected candidate")
                if item.selected_order != index:
                    raise CitationContextSelectionError("selected_order must be 1..N ascending")
                sel_ids.add(identity)
            drop_ids: set[tuple[str, str]] = set()
            for item in case.dropped:
                identity = (item.document_id, item.chunk_id)
                if identity not in cand_set:
                    raise CitationContextSelectionError("dropped candidate not in candidate list")
                drop_ids.add(identity)
            if sel_ids & drop_ids:
                raise CitationContextSelectionError("candidate both selected and dropped")
            if (sel_ids | drop_ids) != cand_set:
                raise CitationContextSelectionError("selected+dropped do not cover all candidates")
            if case.eligible and not case.selected_expected_support_ids:
                raise CitationContextSelectionError("eligible case requires expected support ids")
            if not case.eligible and case.selected_expected_support_ids:
                raise CitationContextSelectionError("non-eligible case must not declare expected support ids")
        return self


class ParetoRelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    K: int = Field(ge=1, le=3)
    support_coverage_not_degraded: bool
    noise_reduced: bool
    token_usage_reduced: bool
    pareto_dominates_k4: bool


class CitationContextComparisonReport(BaseModel):
    """WP5 v1 deterministic comparison report (no winner selection)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["citation-context-comparison.v1"] = CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION
    dataset_id: StrictStr
    dataset_version: StrictStr
    dataset_digest: StrictStr
    retrieval_evidence_schema: Literal["no-answer-rrf-evidence.v2"]
    retrieval_evidence_digest: StrictStr
    policy_ref: Literal["fixed-top-k.v1"]
    baseline_ref: Literal["fixed-top-k.v1/K=4"] = WP5_COMPARISON_BASELINE
    runtime_context_baseline: Literal["localagent-retrieval-context.v1"] = RUNTIME_CONTEXT_BASELINE
    k4_is_production_context_exact: Literal[False]
    eligible_case_count: int = Field(ge=0)
    per_k: dict[int, CitationContextAggregateMetrics]
    pareto: tuple[ParetoRelation, ...]
    tokenizer_authority: Literal["PINNED_GENERATION_MODEL", "FIXTURE_TEST_ONLY"]
    token_usage_authority: Literal["PINNED_GENERATION_MODEL_TOKENIZER_ON_WP5_SERIALIZED_RAG_CONTEXT"] = (
        TOKEN_USAGE_AUTHORITY
    )
    citation_correctness: Literal["NOT_EVALUATED_IN_WP5_V1"] = NOT_EVALUATED_IN_WP5_V1
    citation_completeness: Literal["NOT_EVALUATED_IN_WP5_V1"] = NOT_EVALUATED_IN_WP5_V1
    real_retrieval: Literal["NOT_RUN"] = NOT_RUN
    real_generation: Literal["NOT_RUN"] = NOT_RUN

    @field_validator("dataset_digest", "retrieval_evidence_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _sha256(value)


def compute_pareto(
    aggregates: Mapping[int, CitationContextAggregateMetrics],
) -> tuple[ParetoRelation, ...]:
    """Pareto relation of each K in {1,2,3} relative to K=4.

    K dominates K=4 iff Context Support Coverage (macro) does not drop AND at
    least one noise metric drops AND serialized token usage drops.
    """
    base = aggregates[4]
    results: list[ParetoRelation] = []
    for k in (1, 2, 3):
        agg = aggregates[k]
        coverage_not_degraded = (
            agg.support_coverage_macro is not None
            and base.support_coverage_macro is not None
            and agg.support_coverage_macro >= base.support_coverage_macro
        )
        noise_chunk_reduced = (
            agg.noise_by_chunk_micro is not None
            and base.noise_by_chunk_micro is not None
            and agg.noise_by_chunk_micro < base.noise_by_chunk_micro
        )
        noise_token_reduced = (
            agg.noise_by_token_micro is not None
            and base.noise_by_token_micro is not None
            and agg.noise_by_token_micro < base.noise_by_token_micro
        )
        noise_reduced = noise_chunk_reduced or noise_token_reduced
        token_reduced = agg.total_serialized_context_tokens < base.total_serialized_context_tokens
        results.append(
            ParetoRelation(
                K=k,
                support_coverage_not_degraded=coverage_not_degraded,
                noise_reduced=noise_reduced,
                token_usage_reduced=token_reduced,
                pareto_dominates_k4=coverage_not_degraded and noise_reduced and token_reduced,
            )
        )
    return tuple(results)


# ---------------------------------------------------------------------------
# Privacy scan (sidecar/report must not leak plaintext)
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = (
    "query",
    "query_plaintext",
    "chunk_text",
    "snippet",
    "document_text",
    "local_path",
    "prompt",
    "model_output",
    "raw_exception",
    "credential",
    "page_content",
)


def privacy_safe_serialization(value: object) -> bool:
    """Recursively scan a sidecar/report object for plaintext key leakage."""

    def walk(node: object) -> bool:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in _FORBIDDEN_KEYS:
                    return False
                if not walk(item):
                    return False
            return True
        if isinstance(node, (list, tuple)):
            return all(walk(item) for item in node)
        return True

    return walk(value)


__all__ = [
    "CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION",
    "CITATION_CONTEXT_SELECTION_SCHEMA_VERSION",
    "DROP_DUPLICATE_CONTENT",
    "DROP_MATERIALIZATION_INVALID",
    "DROP_RANK_AFTER_K",
    "EXPECTED_CHUNK_MANIFEST_DIGEST",
    "EXPECTED_GGUF_SHA256",
    "EXPECTED_SOURCE_MANIFEST_DIGEST",
    "FIXTURE_TOKENIZATION_MODE_REF",
    "FIXTURE_TOKENIZER_IDENTITY",
    "FIXTURE_TOKENIZER_REF",
    "GENERATION_MODEL_REF",
    "GENERATION_TOKENIZER_REF",
    "K_VALUES",
    "LLAMA_CPP_VERSION",
    "MAX_K",
    "NOT_EVALUATED_IN_WP5_V1",
    "NOT_RUN",
    "POLICY_REF",
    "RUNTIME_CONTEXT_BASELINE",
    "SERIALIZER_REF",
    "SERIALIZER_VERSION",
    "TOKEN_USAGE_AUTHORITY",
    "TOKENIZATION_MODE_REF",
    "TOKENIZE_ADD_BOS",
    "TOKENIZE_SPECIAL",
    "WP5_COMPARISON_BASELINE",
    "WP5_DEDUP_IS_PRODUCTION_EXACT",
    "WP5_DEDUP_REF",
    "CandidateView",
    "CaseCandidateView",
    "CaseSelectionResult",
    "CitationContextAggregateMetrics",
    "CitationContextCandidate",
    "CitationContextCaseMetrics",
    "CitationContextCaseMetricsBuilder",
    "CitationContextCaseSidecar",
    "CitationContextComparisonReport",
    "CitationContextDropped",
    "CitationContextMaterializationError",
    "CitationContextProtocolError",
    "CitationContextSelected",
    "CitationContextSelectionEnvelope",
    "CitationContextSelectionError",
    "CitationContextSerializer",
    "CitationContextTokenizerError",
    "ControlledCorpus",
    "ControlledCorpusEntry",
    "DroppedView",
    "FixtureTokenCounter",
    "FixedTopKSelector",
    "LlamaCppTokenCounter",
    "ParetoRelation",
    "SelectedView",
    "SourceFile",
    "SourceManifest",
    "TokenCounter",
    "TokenizerIdentityMismatch",
    "aggregate_case_metrics",
    "canonical_digest",
    "compute_pareto",
    "count_selection_tokens",
    "ordered_chunk_manifest_digest",
    "privacy_safe_serialization",
    "source_manifest_digest",
    "validate_external_source_authority",
    "verify_materialized_sources",
    "verify_tokenizer_file",
]
