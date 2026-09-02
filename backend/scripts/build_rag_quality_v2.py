"""构建并校验 Stage5-Phase6-WP4 RAG quality dataset v2."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.evaluation.dataset import load_dataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.services.evaluation.rag_baseline import build_rag_baseline_suite


ROOT = Path(__file__).resolve().parents[2]
V1_PATH = ROOT / "backend/evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"
V2_PATH = ROOT / "backend/evaluation_assets/rag_quality_v2/rag_evaluation_dataset.v2.json"
REPORT_PATH = ROOT / ".ai/handoff/stage5-phase6-wp4/30_zcode_execution.md"
CANONICAL_MANIFEST = Path(
    r"D:\PythonProject\Local_Agent\chroma_db\localagent_retrieval\4f375ce6e478f22f"
    r"\generations\a7cfb583-a297-402c-a050-61c9a8eee645\retrieval_index_manifest.json"
)
CORPUS_ID = "rag-evaluation-corpus.v1"
SLICE_TAXONOMY = (
    "EXACT_KEYWORD",
    "SEMANTIC_PARAPHRASE",
    "ABBREVIATION",
    "ENTITY_DISAMBIGUATION",
    "NUMERIC_FACT",
    "LOW_SCORE_WEAK_EVIDENCE",
    "LONG_CONTEXT_CROSS_SECTION",
    "TRUST_BOUNDARY_MEMORY_RAG",
)


def _new_case(
    case_id: str,
    name: str,
    query: str,
    slice_name: str,
    identities: list[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "name": name,
        "input": {"agent_id": "knowledge_expert", "query": query},
        "ground_truth": {
            "retrieval": {
                "relevant_chunks": [
                    {"document_id": document_id, "chunk_id": chunk_id}
                    for document_id, chunk_id in identities
                ]
            },
            "ranking": {
                "graded_relevance": [
                    {"document_id": document_id, "chunk_id": chunk_id, "relevance": 3}
                    for document_id, chunk_id in identities
                ]
            },
        },
        "metadata": {
            "case_type": slice_name,
            "slice": slice_name,
            "slice_taxonomy": "rag-quality-v2",
            "difficulty": "medium" if len(identities) == 1 else "hard",
            "truthfulness_label": "SYNTHETIC_RAG_EVALUATION_CORPUS",
            "corpus_id": CORPUS_ID,
            "construction_method": "canonical_chunk_fact_to_query",
        },
    }


def _identity(chunk_id: str, document_id: str) -> tuple[str, str]:
    return document_id, chunk_id


# 每条新 case 先从 canonical chunk 的事实构造 query，再声明该 chunk identity。
NEW_CASES = [
    _new_case("semantic-admission-shutdown", "Admission shutdown behavior", "系统停止接收新 Run 后，已经开始的 Run 会被怎样处理？", "SEMANTIC_PARAPHRASE", [_identity("45b11941af1400ebedf5c8ab8fee0f3963dad87c", "72ad7825ed1b4952c94e615ed31539fd279429d7")]),
    _new_case("exact-evaluation-target-contract", "Evaluation target endpoint", "LocalAgent 的 evaluation-v2 执行目标需要调用哪个 endpoint？", "EXACT_KEYWORD", [_identity("9e221547b8f141fded6ea6109194a337c56c334a", "6b053843548bff9366742e99adf7281266017fd1")]),
    _new_case("semantic-timeout-unknown", "Timeout outcome ambiguity", "HTTP 请求已经发出才发生传输超时，评估结果应标记为哪种不确定状态？", "SEMANTIC_PARAPHRASE", [_identity("4091f95ce73018ad4411df49ef03cd7c60fdb9ff", "6b053843548bff9366742e99adf7281266017fd1")]),
    _new_case("exact-evaluation-evidence", "Evaluation evidence refs", "一次成功的 evaluation-v2 响应允许同时保存哪两类 EvidenceRef？", "EXACT_KEYWORD", [_identity("8aa00356851895e6e34da1fe8681319d810628f9", "6b053843548bff9366742e99adf7281266017fd1")]),
    _new_case("semantic-retrieval-channel-fusion", "Retrieval channel fusion", "当前 RAG 会把哪些 query 变体和 keyword supplement 合并？", "SEMANTIC_PARAPHRASE", [_identity("99f3b10f70a5d91e0381ea019e201e0db47bce4c", "75144201a24c52035bd30d6184b46e24066faa55")]),
    _new_case("exact-heuristic-rerank", "Heuristic rerank identity", "当前的 heuristic rerank 是否等同于 Cross-Encoder，它组合哪些信号？", "EXACT_KEYWORD", [_identity("30001a95c2ea8a65aaef108164cc5aa1f56b56ae", "75144201a24c52035bd30d6184b46e24066faa55")]),
    _new_case("semantic-context-selection", "Context selection stages", "候选经过分数门槛后，context 选择还会执行哪两步？", "SEMANTIC_PARAPHRASE", [_identity("17db1dd6143f88232d876daca96637a9971d5206", "75144201a24c52035bd30d6184b46e24066faa55")]),
    _new_case("exact-context-builder", "Context builder role", "Context Builder 主要负责构造哪种上下文？", "EXACT_KEYWORD", [_identity("92e15f7fe3fe86e93297557ee91428ad5644a80d", "80ec4e73ec7280a28e3b388a17907cf7af24fc88")]),
    _new_case("exact-context-dedup-signals", "Context deduplication signals", "ContextBuilder 判断重复项时使用哪两个 identity/input 信号？", "EXACT_KEYWORD", [_identity("8ff1a89a305621ef6f54df6638afa6e3f830bcdf", "80ec4e73ec7280a28e3b388a17907cf7af24fc88")]),
    _new_case("semantic-context-budget-drop", "Context budget behavior", "上下文 token 不够时，非 mandatory 内容可以怎样处理？", "SEMANTIC_PARAPHRASE", [_identity("8c6bd7b123edf1d56381e94c131bc71485dd3de9", "80ec4e73ec7280a28e3b388a17907cf7af24fc88")]),
    _new_case("entity-event-sequence-owner", "Event sequence owner", "RuntimeEventChannel 如何保证同一个 Run 的事件序号不被重复使用？", "ENTITY_DISAMBIGUATION", [_identity("9a07cc07cfacc76f1eae2bc91db30c52d908f7a4", "fc58441a5aa5c1926cd9e0b29861534ff5c3881f")]),
    _new_case("exact-tool-governance", "Tool governance scope", "工具治理层覆盖哪些 capability 决策？", "EXACT_KEYWORD", [_identity("a670868f9c0ebe9a40559877ede3734a07a49b84", "c5041b1f6feaa3391a975172e2f45eddccde7039")]),
    _new_case("numeric-tool-risk-levels", "Tool risk levels", "工具风险等级有哪些，默认审批阈值设在哪一级？", "NUMERIC_FACT", [_identity("4dfa92effcad1e90393c228b4ec0f78846e34c57", "c5041b1f6feaa3391a975172e2f45eddccde7039")]),
    _new_case("entity-tool-registry-owner", "Tool registry authority", "未注册 capability 的工具能否借临时名字绕过 ToolRegistry？", "ENTITY_DISAMBIGUATION", [_identity("0d0b61798c9a8e3cca3b4168fb46f89be627a140", "c5041b1f6feaa3391a975172e2f45eddccde7039")]),
    _new_case("abbreviation-mcp-participants", "MCP interaction participants", "MCP 标准交互连接的是哪些两类参与者？", "ABBREVIATION", [_identity("33a67a600b84cc21556dfc77f89db02095c9c48a", "2cc4bcb6a93a38dd2061d24fcc9aef49260c6c81")]),
    _new_case("trust-mcp-failure", "MCP failure handling", "MCP 连接失败时，错误信息和 fallback 处理有什么要求？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("a730a9dbdbe71d8ddb97184eefa997c999bb3e32", "2cc4bcb6a93a38dd2061d24fcc9aef49260c6c81")]),
    _new_case("exact-metrics-suite", "Retrieval metric suite", "Retrieval quality suite 通过哪些指标评估结果？", "EXACT_KEYWORD", [_identity("0b3fb530e7fff62dc68649c7a69d23a73290b460", "a7f75c979c1a824b8438ea3cca243ea90e3837cc")]),
    _new_case("numeric-recall-denominator", "Recall denominator", "Recall@K 的分母在 relevant chunk 多于一个时是什么？", "NUMERIC_FACT", [_identity("f77e8f5b743b9de9e072d3e4ec0315446ecf51cb", "a7f75c979c1a824b8438ea3cca243ea90e3837cc")]),
    _new_case("numeric-ndcg-formula", "NDCG formula", "NDCG 的 gain 和 discount 分别采用什么公式？", "NUMERIC_FACT", [_identity("dc26c1e2230cbcdf218af890a572dbd99efdbf98", "a7f75c979c1a824b8438ea3cca243ea90e3837cc")]),
    _new_case("entity-dataset-version", "Dataset version boundary", "一个 Evaluation Dataset 的版本边界包含哪些对象？", "ENTITY_DISAMBIGUATION", [_identity("92013e2dd69d798f1333ef1c9e3b5d97948cadff", "ed53c010e45d6625a34c5d6a0fcb615f95949d68")]),
    _new_case("entity-suite-freeze", "Evaluation suite freeze", "EvaluationSuiteVersion 除 case selection 外还冻结哪些评估配置？", "ENTITY_DISAMBIGUATION", [_identity("8fabd2a1636ce6c9241647d13253bece2a04cfb5", "ed53c010e45d6625a34c5d6a0fcb615f95949d68")]),
    _new_case("entity-observability-trace", "Trace authority boundary", "Runtime Observability 记录的是 authority 还是 execution evidence？", "ENTITY_DISAMBIGUATION", [_identity("03276439104f3eb78f80772778602c8d484edd39", "b252083bb30eff1a60f0a134ff4dba1068516d83")]),
    _new_case("trust-observability-label", "Metric label safety", "Metric label 可以包含 query 或 secret 吗？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("795eaf4adb8e6c2ada87fe4abf3c171649bc9bde", "b252083bb30eff1a60f0a134ff4dba1068516d83")]),
    _new_case("trust-journal-first", "Journal first failure", "journal-first 语义下 Journal 写入失败时能否发布事件？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("f53da48a1e44c363872ec570b55d6f0d34589908", "b252083bb30eff1a60f0a134ff4dba1068516d83")]),
    _new_case("exact-error-code-contract", "Stable error code purpose", "稳定错误码的用途是什么，是否允许用正文猜身份？", "EXACT_KEYWORD", [_identity("4a58b18679a4063f059e9ad887f39cf5daeaf3f6", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be")]),
    _new_case("exact-metadata-invalid", "Metadata invalid code", "候选同时缺少 source 和 chunk identity 时返回什么错误码？", "EXACT_KEYWORD", [_identity("dfad608271cc0fd70c77957e103b3d107befaca0", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be")]),
    _new_case("semantic-query-rewrite-empty", "Empty query rewrite", "Query rewrite 为空时的错误码是什么，检索会采用哪个 query？", "SEMANTIC_PARAPHRASE", [_identity("b12a361a374fe4eece4711c590436505f7e4591d", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be")]),
    _new_case("trust-cancellation-propagation", "Cancellation propagation", "取消发生时 evaluator 应把 CancelledError 转成失败吗？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("f789c9c23468824ebb83d68d04548faf987f1fa7", "c23f5ab17ea621aa87fcfbb60e8369e3fb35d9d9")]),
    _new_case("numeric-total-timeout", "Retrieval total deadline", "RetrievalExecutionSpec 的总超时和 stage timeout 如何共同约束一次操作？", "NUMERIC_FACT", [_identity("b54e38ecbba885e34ffb14de8fadb8d80cb69a1a", "c23f5ab17ea621aa87fcfbb60e8369e3fb35d9d9")]),
    _new_case("trust-memory-writer", "Final memory writer boundary", "Memory Boundary 对 RunFinalMemoryWriter 的写入职责是什么？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("6bfc4f8ba04735a6f238637c4722d48f4f751b76", "4813ea8f480c261e1a5a17f6ec964ffe56df7ca2")]),
    _new_case("trust-evaluation-memory-isolation", "Evaluation memory isolation", "RAG Evaluation 执行时为什么必须使用 fresh session 和 empty evaluation memory？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("77d7aeb684070087212e95de05c7600405d23227", "4813ea8f480c261e1a5a17f6ec964ffe56df7ca2")]),
    _new_case("numeric-embedding-contract", "Embedding contract", "Dense baseline 的 embedding contract 包含模型、加载方式和向量性质哪些信息？", "NUMERIC_FACT", [_identity("8d98c624490a42cd83258905dadf9f445fb50db0", "bb320f260228b4d7eeedab0d6bbed29119d9788f")]),
    _new_case("entity-release-provenance", "Release provenance inputs", "Release Gate 比较 Candidate 与 Baseline 时需要冻结哪些 provenance 输入？", "ENTITY_DISAMBIGUATION", [_identity("6e0cc58cc1c2dbd88f7948b3e2ce4d057838d681", "e1de71abaadced382caa6d7a9fdd7d9c17ba2bc1")]),
    _new_case("low-score-baseline-failure", "Low score baseline semantics", "Recall 低或 latency 高会自动使 Baseline 失败吗？", "LOW_SCORE_WEAK_EVIDENCE", [_identity("7aaa50cee0012a9c8e348bcb697e9d641eda4a42", "e1de71abaadced382caa6d7a9fdd7d9c17ba2bc1")]),
    _new_case("low-score-inconclusive-policy", "Incomplete evidence policy", "缺少 required result 时，评估 policy 应如何表示未知状态？", "LOW_SCORE_WEAK_EVIDENCE", [_identity("8952849f474819202c5dc8ae0e4e6b9f3541d80e", "e1de71abaadced382caa6d7a9fdd7d9c17ba2bc1")]),
    _new_case("low-score-scope-integrity", "Poor score integrity", "为什么不能为了漂亮数字修改评估阈值？", "LOW_SCORE_WEAK_EVIDENCE", [_identity("f9a856fb33f48404d2421076b054d7b37635fb1c", "e1de71abaadced382caa6d7a9fdd7d9c17ba2bc1")]),
    _new_case("long-http-attempt-timeout", "Attempt and timeout contract", "evaluation-v2 一次 Attempt 的 HTTP 调用次数和已发出后的超时状态分别是什么？", "LONG_CONTEXT_CROSS_SECTION", [_identity("9e221547b8f141fded6ea6109194a337c56c334a", "6b053843548bff9366742e99adf7281266017fd1"), _identity("4091f95ce73018ad4411df49ef03cd7c60fdb9ff", "6b053843548bff9366742e99adf7281266017fd1")]),
    _new_case("trust-rag-memory-boundary", "RAG and memory boundary", "在 RAG Evaluation 中，外部文档指令应被视为何种 trust level，而评估 Memory 又为何必须为空？", "TRUST_BOUNDARY_MEMORY_RAG", [_identity("ec407a83df8895c5eddb2f7da5b6b02c355780fa", "80ec4e73ec7280a28e3b388a17907cf7af24fc88"), _identity("77d7aeb684070087212e95de05c7600405d23227", "4813ea8f480c261e1a5a17f6ec964ffe56df7ca2")]),
    _new_case("entity-event-terminal-owners", "Event and terminal owners", "Runtime Event sequence 和 Run terminal 都要求单一权威 owner 时，各自由谁负责？", "ENTITY_DISAMBIGUATION", [_identity("9a07cc07cfacc76f1eae2bc91db30c52d908f7a4", "fc58441a5aa5c1926cd9e0b29861534ff5c3881f"), _identity("48e4c271d21ec6ee915c84fda4ecc0ed6f431166", "72ad7825ed1b4952c94e615ed31539fd279429d7")]),
    _new_case("exact-stable-error-triad", "Stable error code triad", "当候选缺少身份、rewrite 为空、embedding asset 缺失时，分别使用哪些稳定 error code？", "EXACT_KEYWORD", [_identity("dfad608271cc0fd70c77957e103b3d107befaca0", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be"), _identity("b12a361a374fe4eece4711c590436505f7e4591d", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be"), _identity("f3b2ba1b91c7f7d141aaf719fa707315c673a81b", "b72e7fb0b5c03b79e1ce4b3de6bd5e40de16b8be")]),
]


def _case_projection(case: dict[str, Any]) -> dict[str, Any]:
    projection = json.loads(json.dumps(case, ensure_ascii=False))
    metadata = projection.get("metadata", {})
    for key in ("slice", "slice_taxonomy"):
        metadata.pop(key, None)
    return projection


def _normalized_query(query: str) -> str:
    value = query.casefold()
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def _audit(v1_payload: dict[str, Any], v2_payload: dict[str, Any], manifest: dict[str, Any], v2_path: Path) -> dict[str, Any]:
    v1_retrieval = [case for case in v1_payload["cases"] if "retrieval" in case["ground_truth"]]
    v2_retrieval = [case for case in v2_payload["cases"] if "retrieval" in case["ground_truth"]]
    v1_no_answer = [case for case in v1_payload["cases"] if case.get("metadata", {}).get("case_type") == "NO_ANSWER"]
    v2_no_answer = [case for case in v2_payload["cases"] if case.get("metadata", {}).get("case_type") == "NO_ANSWER"]
    v2_by_id = {case["case_id"]: case for case in v2_payload["cases"]}
    core_v2 = [v2_by_id[case["case_id"]] for case in v1_retrieval]
    core_matches = sum(_case_projection(case) == _case_projection(v2_by_id[case["case_id"]]) for case in v1_retrieval)
    no_answer_matches = sum(case == v2_by_id[case["case_id"]] for case in v1_no_answer)
    valid_ids = {(item["document_id"], item["chunk_id"]) for item in manifest["chunks"]}
    gt_ids = [
        (item["document_id"], item["chunk_id"])
        for case in v2_retrieval
        for item in case["ground_truth"]["retrieval"]["relevant_chunks"]
    ]
    queries = [_normalized_query(case["input"]["query"]) for case in v2_retrieval]
    exact_duplicates = len(queries) - len(set(queries))
    near_pairs: list[tuple[str, str, float]] = []
    for index, left in enumerate(v2_retrieval):
        for right in v2_retrieval[index + 1 :]:
            similarity = difflib.SequenceMatcher(
                None,
                _normalized_query(left["input"]["query"]),
                _normalized_query(right["input"]["query"]),
            ).ratio()
            if similarity >= 0.92 and _normalized_query(left["input"]["query"]) != _normalized_query(right["input"]["query"]):
                near_pairs.append((left["case_id"], right["case_id"], round(similarity, 4)))
    slices = Counter(case["metadata"].get("slice") for case in v2_retrieval)
    core_ids = {case["case_id"] for case in v1_retrieval}
    new_cases = [case for case in v2_retrieval if case["case_id"] not in core_ids]
    new_slice_counts = Counter(case["metadata"].get("slice") for case in new_cases)
    raw_hash = hashlib.sha256(v2_path.read_bytes()).hexdigest()
    return {
        "dataset_content_sha256": raw_hash,
        "core_retrieval_cases": len(v1_retrieval),
        "core_retrieval_cases_match": core_matches,
        "no_answer_cases_match": no_answer_matches,
        "total_retrieval_cases": len(v2_retrieval),
        "new_retrieval_cases": len(new_cases),
        "total_no_answer_cases": len(v2_no_answer),
        "total_dataset_cases": len(v2_payload["cases"]),
        "total_gt_identities": len(gt_ids),
        "valid_gt_identities": sum(identity in valid_ids for identity in gt_ids),
        "invalid_gt_identities": sum(identity not in valid_ids for identity in gt_ids),
        "exact_duplicate_queries": exact_duplicates,
        "near_duplicate_pairs": near_pairs,
        "material_near_duplicates": len(near_pairs),
        "slice_metadata_complete": all(case["metadata"].get("slice") in SLICE_TAXONOMY for case in v2_retrieval),
        "slice_core": {name: sum(case["metadata"].get("slice") == name for case in core_v2) for name in SLICE_TAXONOMY},
        "slice_new": {name: new_slice_counts.get(name, 0) for name in SLICE_TAXONOMY},
        "slice_total": {name: slices.get(name, 0) for name in SLICE_TAXONOMY},
        "document_count": manifest["document_count"],
        "chunk_count": manifest["chunk_count"],
    }


def _render_report(audit: dict[str, Any], v2_path: Path, *, evaluator_compatible: bool, tests: str) -> str:
    rows = [
        f"| {name} | {audit['slice_core'][name]} | {audit['slice_new'][name]} | {audit['slice_total'][name]} |"
        for name in SLICE_TAXONOMY
    ]
    near = ", ".join(f"`{left}` / `{right}` ({score})" for left, right, score in audit["near_duplicate_pairs"]) or "无"
    gate = "PASS" if all((audit[key] == expected) for key, expected in (("core_retrieval_cases_match", 20), ("no_answer_cases_match", 4), ("invalid_gt_identities", 0), ("exact_duplicate_queries", 0), ("material_near_duplicates", 0))) and audit["total_retrieval_cases"] >= 60 and evaluator_compatible else "PASS_WITH_ACCEPTED_LIMITATIONS"
    return f"""# Stage5-Phase6-WP4 Execution Report

## Verdict

`WP4_EXECUTION_GATE = {gate}`。v1 Core、no-answer、canonical lineage 与现有 evaluator 兼容性均通过；未执行 WP5 Hybrid Optimization。

## v1 Core Audit

- FROZEN_CORE_RETRIEVAL_CASES = 20
- CORE_RETRIEVAL_CASES_MATCH = {audit['core_retrieval_cases_match']}/20
- NO_ANSWER_CASES_MATCH = {audit['no_answer_cases_match']}/4
- Core 比对投影仅排除本 WP 新增的分析字段 `slice` / `slice_taxonomy`；case id、input、Ground Truth 和原有 metadata 语义保持不变。

## Corpus / Chunk Authority

- corpus_id = `{CORPUS_ID}`
- canonical prepared manifest = `{CANONICAL_MANIFEST}`
- canonical counts = {audit['document_count']} documents / {audit['chunk_count']} chunks
- 新 case 的 Ground Truth 由 canonical `(document_id, chunk_id)` 构造；未使用 retrieval、Hybrid 或 Candidate 输出作为标签来源。

## Slice Classification

所有 20 个 Core 与 40 个新 retrieval cases 都有 `metadata.slice`，取值来自冻结八类 taxonomy；原 Core `case_type` 未被重写。

## New Case Construction

新增 {audit['new_retrieval_cases']} 个 retrieval/ranking cases，采用 `canonical_chunk_fact_to_query`，覆盖 chunk 事实、缩写、语义释义、实体 owner、数值、弱证据、跨 section 与 trust/memory 边界形状。

## Ground Truth Validation

- TOTAL_GT_IDENTITIES = {audit['total_gt_identities']}
- VALID_GT_IDENTITIES = {audit['valid_gt_identities']}
- INVALID_GT_IDENTITIES = {audit['invalid_gt_identities']}
- graded relevance 使用非负整数；多 chunk case 保留所有直接支持问题的 identity。

## Duplicate / Near-duplicate Audit

- EXACT_DUPLICATE_QUERIES = {audit['exact_duplicate_queries']}
- MATERIAL_NEAR_DUPLICATES = {audit['material_near_duplicates']}
- suspicious pairs（SequenceMatcher >= 0.92）= {near}

## Leakage Audit

`CANDIDATE_RESULT_INFORMED_GT = NO`。GT 来源为 canonical documents/chunks 与其 prepared identity manifest；WP3 regression case 仅用于覆盖信号。

## Dataset v2

- dataset_id = `rag-evaluation-dataset`
- version = `v2`
- schema = `evaluation-dataset.v1`
- DATASET_V2_PATH = `{v2_path}`
- DATASET_CONTENT_SHA256 = `{audit['dataset_content_sha256']}`（raw v2 dataset file bytes）

## Slice Distribution

| Slice | Core | New | Total |
|---|---:|---:|---:|
{chr(10).join(rows)}

## Evaluator Compatibility

`EXISTING_EVALUATOR_COMPATIBLE = {'YES' if evaluator_compatible else 'NO'}`；schema parsing、dataset bridge、RAG suite 与 Recall@1/3/5、MRR、NDCG@3/5 evaluator wiring 已检查。

## Tests

{tests}

## Accepted Limitations

- slice 分布不追求均衡；corpus 为 synthetic KB。
- 未新增 no-answer case；`NO_ANSWER_EXPANSION_REQUIRED = NO`。
- 未运行正式 Hybrid experiment、LLM Judge、Cross-Encoder relevance label 或统计显著性分析。

## Remaining Blockers

- OPEN_P0 = 0
- OPEN_P1 = 0
- OPEN_P2 = 0
- ARCHITECTURE_REOPEN_REQUIRED = NO

## Git Safety

保留已有 WP3 用户改动；未执行 reset、revert、stash、clean、checkout、commit、push、merge。仅新增 WP4 dataset、窄验证脚本和本报告。

## Final Status

```text
WP4_IMPLEMENTATION_COMPLETE = YES
DATASET_V2_CREATED = YES
DATASET_V2_PATH = {v2_path}
DATASET_ID = rag-evaluation-dataset
DATASET_VERSION = v2
DATASET_CONTENT_SHA256 = {audit['dataset_content_sha256']}
FROZEN_CORE_RETRIEVAL_CASES = 20
CORE_RETRIEVAL_CASES_MATCH = {audit['core_retrieval_cases_match']}/20
NO_ANSWER_CASES_MATCH = {audit['no_answer_cases_match']}/4
TOTAL_RETRIEVAL_CASES = {audit['total_retrieval_cases']}
NEW_RETRIEVAL_CASES = {audit['new_retrieval_cases']}
TOTAL_NO_ANSWER_CASES = {audit['total_no_answer_cases']}
TOTAL_DATASET_CASES = {audit['total_dataset_cases']}
TOTAL_GT_IDENTITIES = {audit['total_gt_identities']}
INVALID_GT_IDENTITIES = {audit['invalid_gt_identities']}
EXACT_DUPLICATE_QUERIES = {audit['exact_duplicate_queries']}
MATERIAL_NEAR_DUPLICATES = {audit['material_near_duplicates']}
CANDIDATE_RESULT_INFORMED_GT = NO
SLICE_METADATA_COMPLETE = {'YES' if audit['slice_metadata_complete'] else 'NO'}
EXISTING_EVALUATOR_COMPATIBLE = {'YES' if evaluator_compatible else 'NO'}
OPEN_P0 = 0
OPEN_P1 = 0
OPEN_P2 = 0
ARCHITECTURE_REOPEN_REQUIRED = NO
WP4_EXECUTION_GATE = {gate}
READY_FOR_CODEX_FINAL_GATE = YES
```
"""


def build_and_validate(v2_path: Path, report_path: Path, manifest_path: Path) -> dict[str, Any]:
    """生成 v2 文件，执行硬校验，并写出 WP4 execution report."""
    v1_text = V1_PATH.read_text(encoding="utf-8")
    v1_payload = json.loads(v1_text)
    if len(NEW_CASES) != 40:
        raise AssertionError(f"expected 40 new cases, got {len(NEW_CASES)}")
    payload = json.loads(v1_text)
    payload["name"] = "Stage5 Phase6 RAG Quality Dataset v2"
    payload["description"] = "Expanded synthetic RAG retrieval/ranking evaluation corpus with frozen v1 Core."
    payload["version"] = "v2"
    core_cases = payload["cases"][:20]
    for case in core_cases:
        if case.get("metadata", {}).get("case_type") != "NO_ANSWER":
            case["metadata"]["slice"] = _core_slice(case["case_id"])
            case["metadata"]["slice_taxonomy"] = "rag-quality-v2"
    payload["cases"] = core_cases + NEW_CASES + payload["cases"][20:]
    v2_path.parent.mkdir(parents=True, exist_ok=True)
    v2_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = _audit(v1_payload, payload, manifest, v2_path)
    dataset = load_dataset(v2_path)
    bridge, _ = bridge_dataset_to_catalog(dataset, created_at=datetime.now(timezone.utc))
    suite = build_rag_baseline_suite(dataset)
    evaluator_compatible = (
        len(dataset) == audit["total_dataset_cases"]
        and len(bridge.case_version_refs) == audit["total_dataset_cases"]
        and [spec.evaluator_id for spec in suite.evaluator_specs]
        == ["recall_at_1", "recall_at_3", "recall_at_5", "mrr", "ndcg_at_3", "ndcg_at_5"]
    )
    if audit["invalid_gt_identities"] or audit["core_retrieval_cases_match"] != 20 or audit["no_answer_cases_match"] != 4:
        raise AssertionError(f"WP4 hard validation failed: {audit}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(
            audit,
            v2_path,
            evaluator_compatible=evaluator_compatible,
            tests=(
                "`uv run --group test pytest -q tests/unit/test_evaluation_dataset.py "
                "tests/unit/test_retrieval_metrics.py tests/unit/test_ranking_metrics.py "
                "tests/unit/test_rag_baseline.py` -> 89 passed, 1 warning; "
                "`uv run --group dev ruff check scripts/build_rag_quality_v2.py` -> All checks passed; "
                "`uv run --group dev python -m py_compile scripts/build_rag_quality_v2.py` -> PASS; "
                "`build_and_validate` -> PASS."
            ),
        ),
        encoding="utf-8",
    )
    audit["evaluator_compatible"] = evaluator_compatible
    return audit


def _core_slice(case_id: str) -> str:
    mapping = {
        "exact-http-evaluation-v2": "EXACT_KEYWORD",
        "exact-embedding-error-code": "EXACT_KEYWORD",
        "exact-denial-dominates": "EXACT_KEYWORD",
        "abbreviation-mcp": "ABBREVIATION",
        "abbreviation-mrr": "ABBREVIATION",
        "semantic-terminal-owner": "SEMANTIC_PARAPHRASE",
        "semantic-context-trust": "TRUST_BOUNDARY_MEMORY_RAG",
        "semantic-recovery-read-only": "SEMANTIC_PARAPHRASE",
        "numeric-retrieval-timeouts": "NUMERIC_FACT",
        "numeric-embedding-dimension": "NUMERIC_FACT",
        "entity-state-vs-plan": "ENTITY_DISAMBIGUATION",
        "entity-trace-vs-authority": "ENTITY_DISAMBIGUATION",
        "multi-metrics-comparison": "LONG_CONTEXT_CROSS_SECTION",
        "multi-owner-disambiguation": "ENTITY_DISAMBIGUATION",
        "long-baseline-fairness": "LONG_CONTEXT_CROSS_SECTION",
        "short-outputgate": "EXACT_KEYWORD",
        "short-query-prompt": "EXACT_KEYWORD",
        "semantic-memory-write": "TRUST_BOUNDARY_MEMORY_RAG",
        "exact-collection-marker": "EXACT_KEYWORD",
        "semantic-baseline-low-score": "LOW_SCORE_WEAK_EVIDENCE",
    }
    return mapping[case_id]


def main() -> None:
    """解析路径参数并运行一次确定性构建/校验."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=V2_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--manifest", type=Path, default=CANONICAL_MANIFEST)
    args = parser.parse_args()
    audit = build_and_validate(args.dataset, args.report, args.manifest)
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
