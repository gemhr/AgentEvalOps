---
title: "BEIR SciFact Benchmark Attribution"
description: "Attribution and usage boundary for the externally-hosted BEIR SciFact public retrieval benchmark."
icon: "clipboard-list"
---

# BEIR SciFact Public Benchmark Attribution

PandaProbe's Stage5 Phase3 retrieval benchmark uses the **BEIR** benchmark's **SciFact** dataset as an external, read-only public retrieval benchmark for before/after comparison of retrieval pipeline changes (e.g. BM25, hybrid RRF, cross-encoder rerank).

## Attribution

- **Benchmark**: BEIR — *BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models* (Thakur, Reimers, Rücklé, Soyer, Gurevych, 2021, arXiv:2104.08663).
- **Original dataset**: SciFact — *SciFact: A Dataset for Combining Automated Scientific Claim Verification with Unstructured Evidence Retrieval* (Wadden et al., 2020, arXiv:2004.14974).
- The BEIR source code is used as a **read-only format reference only**; the `beir` package is **not** a runtime dependency.

## External asset boundary

The SciFact corpus, queries and qrels are **not vendored** into this repository. They are loaded at runtime from a configured external read-only path (`corpus.jsonl`, `queries.jsonl`, `qrels/test.tsv`). Frozen asset identity (dataset ZIP MD5 and per-file SHA-256 values) is recorded in `backend/app/core/evaluation/beir_scifact.py` and in the Stage5 Phase3 handoffs; every benchmark candidate must use the exact same asset.

## Interpretation boundary

Results produced from this dataset are labeled `BEIR_SCIFACT_LOCALAGENT_ADAPTED`: original BEIR documents are chunked by the local pipeline, retrieval operates on chunks, and chunk rankings are projected back to document identities before scoring against BEIR qrels. These numbers are **not** official BEIR leaderboard results and must not be compared directly against public leaderboard scores; their authoritative use is same-pipeline before/after comparison within this project.
