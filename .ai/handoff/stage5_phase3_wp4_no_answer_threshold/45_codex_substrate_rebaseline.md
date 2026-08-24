# Stage5-Phase3-WP4 Frozen Retrieval Substrate Rebaseline — Codex Architecture Decision

> Phase: H — Architecture Decision
> Date: 2026-08-24
> Scope: 只解决 `FROZEN_CACHE_IDENTITY_CORPUS_MISMATCH`；本文件不证明 cache 已构建、evidence 已生成或 real calibration/evaluation 已执行。

# 1. Decision

```text
SUBSTRATE_REBASELINE = APPROVED
OPTION = A

KEEP_DATASET = YES
KEEP_CORPUS = YES
KEEP_RRF_ALGORITHM = YES
PROVISION_SYNTHETIC_DENSE_CACHE = YES
PROVISION_SYNTHETIC_BM25_CACHE = YES

WP4_DENSE_CACHE_IDENTITY = TO_BE_PROVISIONED_AND_FROZEN
WP4_BM25_CACHE_IDENTITY = TO_BE_PROVISIONED_AND_FROZEN

REAL_CALIBRATION_EVALUATION_ALLOWED = NO
WP5_ALLOWED = NO
```

批准 Option A：保留 `no-answer-threshold-dataset.v2` 与 `rag-evaluation-corpus.v1`，为同一 15-document / 60-chunk corpus provision 正确的 Dense/BM25 READY caches，实际取得 identities 后再对齐 AgentEvalOps WP4 contract。

方案比较：

| Option | Decision | Reason |
|---|---|---|
| A — synthetic corpus + 新建对应 caches | **APPROVED** | 保留已经冻结的 answerability Ground Truth；修复的是 substrate reality，而不是改写 Dataset 或弱化复现性。 |
| B — Dataset/corpus 改为 SciFact | REJECTED | SciFact 300 全部有 positive qrels，不能保持当前 ANSWERABLE/EMPTY/WEAK/MISLEADING Authority；这会变成 Dataset redesign。 |
| C — 移除/弱化 cache identity invariant | REJECTED | threshold 必须绑定 corpus/chunks/index/model/config；弱化后无法证明重建 index 仍可复用 threshold。 |
| D — 其它 | NOT_NEEDED | 没有比 A 更小且同时保留 Dataset Authority 与 strict reproducibility 的正确方案。 |

Option A 属于 `EXPERIMENT_SUBSTRATE_PROVISIONING`，不是 `RETRIEVAL_ALGORITHM_CHANGE`。算法、channel semantics、embedding model/config、chunking、BM25 formula/tokenizer、RRF 与 budgets 全部保持时，WP4 的唯一实验变量仍是 No-Answer policy。

但 current source fresh audit 证明，A 不能直接作为 M 级“运行现有命令”实施：synthetic Dense/BM25 路径没有与 SciFact 同等级的 READY cache identity lifecycle。后续必须先进行一次受限的 LocalAgent H 级 cache-lifecycle implementation；不得在 provisioning 脚本中另造 identity 或第二套 retrieval implementation。

# 2. Root Cause

当前 WP4 同时冻结了两个互不相容的事实：

```text
Dataset corpus = rag-evaluation-corpus.v1
Dataset corpus size = 15 documents / 60 deterministic chunks

Frozen Dense identity = b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46
Frozen BM25 identity  = 594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b
```

Fresh metadata 证明两个 frozen identities 均属于：

```text
corpus_id = beir-scifact-corpus.v1
dataset = scifact
document_count = 5183
Dense chunk_count = 9548
Dense collection = beir_scifact_eval_v1
```

Dense identity function `beir_scifact_cache_identity()` 的 canonical payload 硬编码 `benchmark=beir`、`dataset=scifact`、`split=test`。BM25 identity/function/environment 同样硬编码 SciFact，并要求 `benchmark_document_id`。因此这些 identities 不是可替换标签，而是另一个 corpus/manifest 的真实 cache facts。

WP4 `RrfEvidenceEnvelope` 与 `FrozenRrfConfig` 又把上述 SciFact keys 写成 exact `Literal`。结果是：对 synthetic corpus 真实构建出的 cache 不可能满足现有 v1 evidence contract；强行使用现有 keys 又不可能检索 WP4 corpus。该矛盾发生在 real evidence preflight，尚未进入 calibration 或 quality Gate。

# 3. Frozen Dataset / Corpus

以下 Authority 保持不变：

```text
DATASET_ASSET_REF = no-answer-threshold-dataset.v2
DATASET_ID = no-answer-threshold-dataset
DATASET_VERSION = v2
DATASET_SCHEMA_VERSION = evaluation-dataset.v4
DATASET_DIGEST = e0042be4e1611eddc209159c8bd598dfa0637285a9b96a237f053a00fad8f9dd

CORPUS_REF = rag-evaluation-corpus.v1
CORPUS_DOCUMENT_COUNT = 15
CORPUS_CHUNK_COUNT = 60
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180

CALIBRATION = ANSWERABLE 4 / EMPTY 4 / WEAK 4 / MISLEADING 2
EVALUATION = ANSWERABLE 4 / EMPTY 4 / WEAK 4 / MISLEADING 2
CONFLICT = 0
```

Fresh read-only probe of `prepare_evaluation_chunks(default_corpus_dir())` returned 15 documents、60 unique chunk IDs；当前 8 个 ANSWERABLE support fact IDs 全部存在，missing `[]`。本 Decision 不修改 query、label、split、support fact、leakage group、Dataset schema/version/digest 或 corpus content。

# 4. Frozen Retrieval Algorithm

以下 retrieval semantics 继续冻结：

```text
RRF_ALGORITHM_REF = rrf.v1
RRF_K = 60
CURRENT_CHANNEL_REF = current-dense-led-ranked.v1
BM25_CHANNEL_REF = bm25-lucene-idf.v1
PER_CHANNEL_CANDIDATE_LIMIT = 8
PRE_FUSION_UNION_LIMIT = 16
FINAL_FUSED_CANDIDATE_LIMIT = 8

BM25_ALGORITHM_REF = bm25-lucene-idf.v1
BM25_TOKENIZER_REF = bm25-unicode-lexical-tokenizer.v1
BM25_K1 = 1.2
BM25_B = 0.75

DENSE_MODEL_REF = Qwen3-Embedding-0.6B
DENSE_DIMENSION = 1024
DENSE_LOCAL_FILES_ONLY = true
DENSE_NORMALIZE_EMBEDDINGS = true
DENSE_QUERY_PROMPT_NAME = ""
```

`VectorDBManager` 的真实 builder 使用 CPU、`local_files_only=True`、normalized embeddings，query prompt 来自 `LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME`（默认空），model path 来自 `Settings.embedding_model_path`。Provisioning 必须显式验证 prompt 为空、dimension 1024、model ref 一致；不得依赖调用环境的偶然默认值。

Current channel 仍是现有 Dense-led ranked semantics，不重解释为 pure Dense；BM25 仍使用现有 tokenizer/formula；RRF 继续只消费两个 channel 的 rank，不混合 raw score，不增加 CE。

# 5. Cache Identity Decision

```text
WP4_DENSE_CACHE_SCHEMA = rag-evaluation-dense-index-cache.v1
WP4_BM25_CACHE_SCHEMA = rag-evaluation-bm25-index-cache.v1
WP4_DENSE_CACHE_IDENTITY = TO_BE_PROVISIONED_AND_FROZEN
WP4_BM25_CACHE_IDENTITY = TO_BE_PROVISIONED_AND_FROZEN
IDENTITY_FREEZE_BEFORE_REALITY = FORBIDDEN
```

本 Decision 不猜测新 hash。Identity 必须由 LocalAgent cache Owner 的 production identity function 对实际 prepared corpus/manifest/config 机械计算，并在完整 READY artifact 原子发布和 warm validation 后冻结。

Dense identity payload 至少绑定：

- cache schema；
- `corpus_id=rag-evaluation-corpus.v1`；
- canonical corpus source-manifest digest；
- ordered chunk-manifest digest；
- model ref、dimension、offline/local-only、normalized embedding、query prompt；
- splitter identity、chunk size/overlap；
- collection/index semantic identity。

BM25 identity payload 至少绑定：

- cache schema、corpus id/digest、同一 ordered chunk-manifest digest；
- `bm25-lucene-idf.v1`、tokenizer ref、k1、b。

Candidate limit 是 query-time RRF/channel contract，不进入 Dense/BM25 cache key；embedding batch size 是 execution tuning，不改变 vector semantics，也不进入 identity。两者仍应记录在 local build provenance 中。

Corpus source-manifest digest 必须从按 relative source path 稳定排序的文件 content digests 机械派生；chunk-manifest digest 必须从 deterministic ordered identities/content hashes 派生。不得以目录路径、mtime、Python `repr` 或手填字符串充当 identity。

# 6. Dense Provisioning Path

当前可复用组件：

- `evaluation_environment.prepare_evaluation_chunks(default_corpus_dir())`：synthetic corpus/chunk Authority；
- `evaluation_environment.build_evaluation_kb()`：使用现有 `VectorDBManager`、model/config、collection `rag_evaluation_kb_v1` 构建 fresh index；
- `VectorDBManager`：唯一 Dense collection/embedding implementation；
- SciFact `build_or_reuse_beir_scifact_cache()`：READY/staging/atomic publish/fail-closed lifecycle 的现有模式。

当前缺口：`build_evaluation_kb()` 只要求 fresh directory并返回 manifest；它没有 cache key、cache schema、READY metadata、atomic publish、warm validation或 load-by-identity。SciFact cache lifecycle 不能直接对 synthetic corpus调用，因为其 identity、metadata、collection和 manifest shape硬编码 BEIR/SciFact。

因此后续 Phase A 必须由 LocalAgent 现有 `evaluation_environment` / Dense cache Owner 做最小 H 级扩展：复用 `prepare_evaluation_chunks()`、`build_evaluation_kb()` 和 `VectorDBManager`，增加 synthetic 专用 deterministic identity、staging→READY atomic lifecycle、strict metadata/collection validation、warm reuse 与 load。禁止复制 embedding/retrieval逻辑或把 AgentEvalOps 变成 Dense cache Owner。

真实 cache 只允许写入明确的 local evaluation cache root，不写 production `chroma_db`，不覆盖现有 collection/cache，不提交 Git。路径属于 local execution input，不写入 shared evidence。

# 7. BM25 Provisioning Path

当前可复用组件：

- `Bm25SparseIndex`：BM25 formula/tokenizer/build/load/search 唯一 Owner；
- `bm25_evaluation_runtime._synthetic_index()`：证明同一 60 chunks 可用现有 `Bm25SparseIndex.build()`；
- SciFact BM25 environment：manifest exact-match、READY metadata、index/manifest digests、atomic publish、warm load 的现有模式。

当前缺口：synthetic `serve-synthetic` 每次只在内存构建 index，输出 `READY + chunk_count`，没有 persistent cache、cache key、Dense manifest exact-match 或 READY artifact。SciFact BM25 environment硬编码 BEIR/SciFact，不能把 synthetic inputs伪装成 SciFact cache。

Phase A 必须由 LocalAgent BM25/cache Owner 增加最小 synthetic READY cache path，继续调用同一 `Bm25SparseIndex.build/save/load`，并要求 BM25 ordered chunk identities 与新 Dense manifest逐项 exact equal。不得调整 tokenizer、k1、b、positive-score filter、tie-break或 candidate semantics；不得在 WP4 evidence generator 中临时序列化另一套 sparse index。

# 8. Evidence Producer

```text
RRF_COMPONENT_OWNER = LocalAgent HybridRrfRetriever
RRF_RUNTIME_OWNER = LocalAgent HybridRrfEvaluationService
WP4_RUNNER_ROLE = EVIDENCE_CONSUMER_ONLY
WP4_EVIDENCE_PRODUCER = THIN_ORCHESTRATOR_OVER_EXISTING_RRF_RUNTIME
```

最合理的真实 producer 是现有 `HybridRrfEvaluationService`：它顺序调用 Current endpoint 与 BM25 endpoint，再用 `HybridRrfRetriever(rrf_k=60)` 生成 fused top-8 和 JSONL provenance。它已输出 query digest、两个 channel rankings、每个 fused chunk 的 channel ranks、RRF score/rank/source channels/channel count与 latency。

现有 `run_hybrid_rrf_evaluation.py` 不能原样充当 WP4 producer：它面向 SciFact +旧 synthetic rag-quality benchmark、依赖数据库 EvaluationLoop，并且 synthetic report 不绑定 synthetic cache identities，也不输出 `no-answer-rrf-evidence` envelope。

Contract alignment 阶段允许增加一个极薄 WP4 evidence-generation orchestration：

```text
Dataset v2 queries
-> existing LocalAgent synthetic Current READY endpoint
-> existing LocalAgent synthetic BM25 READY endpoint
-> existing HybridRrfEvaluationService (CE disabled)
-> strict no-answer RRF evidence envelope
```

该 orchestration 只负责：读取 Dataset、分配 run/artifact identity、调用 endpoint、对齐 query digest与 case id、验证 READY cache metadata和 provenance、投影 strict envelope并安全落盘。不得实现 Dense/BM25/RRF、不得手算或改写 score、不得读取 label决定 retrieval、不得在 WP4 consumer runner 中加入 retrieval。

未来正式 evidence 必须覆盖全部 28 cases，来自真实 synthetic caches；deterministic fixture、mock、手填 score、旧 SciFact evidence均禁止。

# 9. Versioning Decision

```text
WP4_RRF_SUBSTRATE_REF = wp4-no-answer-rrf-substrate.v2
RRF_EVIDENCE_SCHEMA = no-answer-rrf-evidence.v2
ACCEPTANCE_GATE_REF = WP4_NO_ANSWER_ACCEPTANCE_GATE.v3
REPORT_SCHEMA = no-answer-threshold-report.v3

DATASET_SCHEMA_CHANGE = NO
DATASET_VERSION_CHANGE = NO
NO_ANSWER_DECISION_SCHEMA_CHANGE = NO
QUALITY_CONDITION_CHANGE = NO
```

必须增加显式 substrate ref；它绑定 Dataset/corpus、两个新 cache identities、channel refs、RRF constants/budgets与 no-CE/no-new-model/read-only facts。

Evidence 必须从 v1 升 v2。原因是当前 `no-answer-rrf-evidence.v1` 的 wire semantics 通过 exact Literals冻结了 SciFact cache keys；静默替换 Literals会重写已冻结 contract。v1 保留历史 parse/validation语义，不接受 synthetic新 keys；v2 增加 `substrate_ref=wp4-no-answer-rrf-substrate.v2` 并在真实 identities存在后冻结它们。

Gate 从 v2 升 v3。虽然 quality formula、coverage和 calibration算法完全不变，但 current public Gate v2 直接消费 v1 `RrfEvidenceEnvelope/FrozenRrfConfig` 并把旧 exact cache identities作为 hard invariant。让同一 Gate ref静默改为接受 v2 substrate会破坏 provenance。Gate v2保留历史行为；Gate v3消费 evidence/substrate v2，并复用 v2全部 Dataset、strict evidence、gate-owned calibration、context、per-case和 quality逻辑。

`no-answer-decision.v1` 不含 cache identity且 policy ref/lock会绑定新 substrate，可保持。Report必须升 v3，因为其公开字段嵌入新的 evidence schema和 Gate ref。受 substrate identity影响的 validated-experiment/config/context/lock proof必须显式携带 substrate ref或升级其 schema标识；不得仅更换常量名。

# 10. Historical Identity Status

```text
WP4_PREVIOUS_DENSE_CACHE_IDENTITY = b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46
WP4_PREVIOUS_BM25_CACHE_IDENTITY = 594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b

WP4_STATUS = INVALID_FOR_WP4_SYNTHETIC_SUBSTRATE
WP4_REASON = BELONGS_TO_BEIR_SCIFACT_CORPUS

WP2_WP3_SCIFACT_STATUS = UNCHANGED
CACHE_ARTIFACT_STATUS = NOT_GLOBALLY_INVALID
```

不得删除或改写历史 handoff/cache metadata。上述 caches 对 WP2/WP3 SciFact仍可能完全有效；本 Decision只撤销它们作为 WP4 `rag-evaluation-corpus.v1` substrate identity的资格。

# 11. Allowed Provisioning Actions

批准按 Reality → Contract 的顺序执行：

## Phase A — Synthetic Substrate Capability + Provisioning

1. 单独 H 级任务中，对 LocalAgent现有 cache Owner做第 6、7节的最小 source/test扩展；
2. 从 `prepare_evaluation_chunks(default_corpus_dir())` 产生 exact 60 chunks；
3. 在全新、隔离的 local cache root构建 Dense READY cache；
4. 用 Dense manifest exact-match构建 BM25 READY cache；
5. 机械取得 identities与完整 provenance；
6. warm load复核 `CACHE_HIT` / no re-embedding / no BM25 rebuild；
7. 使用固定 CALIBRATION-only smoke queries做 structural RRF smoke；
8. 不修改 AgentEvalOps frozen contract，不运行 calibration/evaluation。

## Phase B — Contract Alignment

1. 仅在 Phase A identities和provenance已存在且通过 Codex窄审后，最小更新 AgentEvalOps WP4 substrate/evidence/Gate/report versioned contract；
2. 增加薄 evidence producer和 focused tests；
3. 生成完整28-case raw evidence之前，先通过 contract/static/focused gate；
4. 再由单独授权进入 real evidence generation与 calibration/evaluation。

不批准 placeholder/TBD hash进入源码。Phase A的 `TO_BE_PROVISIONED_AND_FROZEN` 只存在于本 Decision状态，不是可接受的 runtime Literal。

# 12. Forbidden Changes

- 修改 Dataset JSON/schema/version/digest、label、split、support fact或corpus content；
- 修改 embedding model、dimension、normalization、prompt、splitter/chunking；
- 修改 BM25 algorithm/tokenizer/k1/b/tie-break；
- 修改 Current channel semantics、RRF algorithm/k/channels/budgets/tie-break；
- 使用 CE、LLM Judge、新模型、Prompt confidence或generation confidence；
- 删除/弱化 cache identity、corpus identity、manifest equality、READY或privacy hard invariant；
- 把 SciFact cache伪装成 synthetic，或修改 SciFact cache metadata/history；
- 在 AgentEvalOps/临时脚本复制 Dense、BM25或RRF实现；
- 在 WP4 consumer runner中执行 retrieval；
- 用 fixture/mock/手填score/旧SciFact evidence生成 real claim；
- 在 Phase A运行 calibration/evaluation、选择 threshold、查看 quality metrics后调 substrate；
- 写 production `chroma_db`、覆盖现有 cache、提交 cache/model/evidence正文；
- 未经新的 H 级任务授权修改 LocalAgent source；
- 进入 WP5、commit或push。

# 13. Required Provenance

Phase A必须在 ignored local evidence中保存、并在handoff中仅摘要安全facts：

## Shared corpus facts

- corpus ref、canonical source-manifest digest、document count；
- splitter identity、chunk size/overlap；
- ordered chunk-manifest digest、chunk count与unique identity count；
- Dataset 8个support IDs全部存在；
- Dense/BM25 ordered chunk identities/content hashes exact equal。

## Dense facts

- cache schema/status/key、collection name/count；
- model ref、dimension、local-files-only、normalization、query prompt；
- embedding compatibility/config digest（不泄露本机model path）；
- corpus/chunk manifest digests；
- cache metadata/manifest file digests、collection metadata exact validation；
- build result、warm hit、`REEMBEDDING=NO`。

## BM25 facts

- cache schema/status/key；
- algorithm/tokenizer/k1/b；
- corpus/chunk manifest digests；
- index/manifest file digests与document/chunk counts；
- Dense manifest exact-match；
- build result、warm hit、`BM25_REBUILD=NO`。

## RRF/evidence facts

- substrate ref、evidence schema、Dataset/corpus/cache identities；
- RRF refs/k/budgets、CE disabled/new model false/runtime read-only；
- per-case query digest、artifact identity、status/counts、fused candidate identity/rank/score/source channels；
- evidence canonical digest与software HEADs；
- shared evidence不含 query/chunk/document plaintext、local path、raw exception、prompt/model output或credentials。

任何 required fact缺失均为 `SUBSTRATE_NOT_READY`，不得猜测、fallback或缩小验证范围。

# 14. Required Smoke Validation

Phase A只允许 structural smoke，不计算 threshold/metrics。固定 smoke population：

```text
cal-answer-terminal-owner
cal-empty-rfc9999
cal-misleading-context-dedup-provenance
```

选择在本 Decision中预先冻结，且全部来自 CALIBRATION split，避免在 policy lock前观察evaluation split行为。Smoke必须验证：

1. Dense/BM25 services均从新 READY caches加载，不走in-memory临时fallback；
2. Hybrid runtime启动时 CE未配置；
3. 每channel `<=8`、union `<=16`、fused `<=8`；
4. chunk identities均存在于同一60-chunk manifest；
5. RRF score可从两个channel ranks按 `Σ1/(60+rank)` exact重算，排序/tie-break符合 frozen contract；
6. status/count/artifact/query digest/provenance完整；
7. smoke outcome只用于 structural readiness，不得修改Dataset、cache config、RRF或No-Answer policy。

Smoke任一失败时停止，不生成28-case evidence，不进入 Phase B contract freeze。

# 15. Contract Alignment Requirements

Phase B必须：

1. 将 Phase A真实 identities写入新的 v2 substrate/evidence contract；
2. 保留 v1 evidence与Gate v2历史行为，不静默改其exact Literals；
3. 引入 explicit `substrate_ref`，并让 evidence、validated invariants、FrozenRrfConfig、policy lock、evaluation context与report全链路exact绑定；
4. 新 Gate v3继续 gate-owned strict raw evidence validation与deterministic calibration re-derivation；
5. Gate v3质量条件与Gate v2完全相同：`FA==0`、`TA>FAb`、strict majority-baseline improvement、technical failures=0、coverage 4/4/4/2；
6. WP4 runner继续只消费 `--dataset/--rrf-evidence/--output-dir`；retrieval producer保持独立；
7. Evidence producer从READY metadata读取expected identities，并交叉验证corpus/chunk manifests，不能由caller任意自报；
8. Dataset digest继续为 `e0042be4...f9dd`；任何变化立即停止并升级；
9. Contract alignment完成并通过 Codex窄 Gate前，`REAL_CALIBRATION_EVALUATION_ALLOWED=NO`。

# 16. Required Tests

## Phase A — LocalAgent H task

- synthetic Dense/BM25 identity相同输入稳定、任一semantic input变化则key变化；
- 15 docs / 60 unique chunks、support IDs存在；
- Dense/BM25 manifest逐项exact equal；
- partial/stale/mismatched metadata、manifest、collection/index digest fail closed；
- cold build→READY、warm hit不re-embed/不rebuild；
- wrong model/dimension/prompt/chunking/BM25 config拒绝；
- service只能加载READY cache，无silent in-memory fallback；
- fixed 3-case structural RRF smoke通过；
- LocalAgent focused tests、Ruff、compileall、`git diff --check`。

## Phase B — AgentEvalOps M task

- v1 evidence保留旧SciFact identity semantics；
- v2只接受actual synthetic identities与substrate v2，拒绝旧SciFact/arbitrary/wrong corpus/config；
- v2 strict case/artifact/candidate/rank/score/privacy tests保持；
- validated proof/lock/context/report exact绑定substrate ref和新identities；
- Gate v2 historical regression保持；Gate v3 valid deterministic path进入相同quality branch；
- Gate v3 forged raw evidence/forged calibration lock继续BLOCKED；
- evidence producer对missing/duplicate/mismatched provenance、wrong READY metadata、plaintext字段fail closed；
- runner `--help`保持no-network side effect；
- focused unit/integration、Ruff、compileall、`git diff --check`。

这些都是 deterministic/structural tests，不得用fixture结果冒充 real 28-case evidence或quality结论。

# 17. Risk

```text
ARCHITECTURE_DECISION_RISK = H
PHASE_A_CURRENT_RISK = H
PHASE_B_CONTRACT_ALIGNMENT_RISK = M
REAL_EXPERIMENT_RISK_AFTER_GATES = M
```

Phase A 不能按原建议直接标 M，因为 current synthetic builders不拥有 persistent READY identity lifecycle；若不改 source只能生成无权威cache hash或临时in-memory index。批准 A 同时明确要求新的 H 级 LocalAgent任务，由现有 Owner做最小能力补齐。

若 H 级 audit发现必须改变 retrieval algorithm、model、chunking、BM25 semantics、Runtime public contract或production composition：

```text
STOP = YES
RETURN_TO_CODEX = YES
OPTION_A_IMPLEMENTATION_ALLOWED = NO_UNTIL_NEW_DECISION
```

若只增加 synthetic cache identity/lifecycle并复用现有components，则不改变single-variable claim；Phase A通过后，AgentEvalOps alignment保持M。

# 18. Next ZCode Instructions

1. 重新读取本 Decision、current source/diff、`50_real_calibration_evaluation_and_gate.md`与两仓规则；
2. 新建单独 H 级 Phase A task/handoff，Scope只含 LocalAgent synthetic Dense/BM25 READY cache identity/lifecycle、focused tests和local provisioning；
3. 先审计并冻结 source-manifest/chunk-manifest canonical payload；不得复制SciFact硬编码或建立通用framework；
4. 在现有 `evaluation_environment`、`VectorDBManager`、`Bm25SparseIndex`与cache lifecycle模式内做最小实现；
5. 构建全新isolated synthetic Dense cache，再以其manifest构建BM25 cache；不碰production `chroma_db`；
6. 保存真实cache metadata/provenance，warm复核；不得先向AgentEvalOps写placeholder/TBD hash；
7. 运行Section 14固定CALIBRATION-only structural smoke；不运行28-case retrieval、calibration或evaluation；
8. 更新Phase A execution handoff，交Codex窄审并冻结真实identities；
9. 只有Codex批准Phase A事实后，创建M级Phase B contract alignment task，实施substrate v2/evidence v2/Gate v3/report v3与薄evidence producer；
10. Phase B focused tests通过后再次Codex Gate；获得单独授权前仍不得生成real 28-case evidence、运行calibration/evaluation或进入WP5；
11. 两仓均不commit/push，除非用户另行明确授权。

当前停止状态：

```text
SOURCE_CHANGE_THIS_DECISION = NONE
TEST_CHANGE_THIS_DECISION = NONE
CACHE_CREATED_THIS_DECISION = NONE
COLLECTION_CREATED_THIS_DECISION = NONE
RRF_EXECUTED_THIS_DECISION = NO
CALIBRATION_EXECUTED_THIS_DECISION = NO
EVALUATION_EXECUTED_THIS_DECISION = NO

REAL_CALIBRATION_EVALUATION = BLOCKED
WP4_CANDIDATE = NOT_EVALUATED_BLOCKED
NEXT_ACTION = PHASE_A_LOCALAGENT_H_TASK
WP5_STARTED = NO
```
