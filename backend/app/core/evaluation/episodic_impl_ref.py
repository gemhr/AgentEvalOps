"""WP6-E AgentEvalOps Episodic evaluation implementation provenance ref。

``AGENTEVALOPS_EPISODIC_EVALUATION_IMPLEMENTATION_REF`` 是对 Episodic evaluation
semantic source manifest 的稳定 content digest（``sha256:<hex>``），算法与 target
``TARGET_EVALUATION_SEMANTIC_SOURCE_FILES`` 一致：``relative-path + NUL + file bytes +
NUL``，stable order。

Manifest 必须覆盖实际 semantic owners（60 Gate）：

- episodic dataset schema/loader、dataset asset、runner、control expansion、capture
  ingestion、symbolic identity resolver、journal evidence、SQLite projection、
  evaluators、metrics、gate、artifact、execution target integration、provisioner /
  transport、baseline、assertion algebra、impl ref。

不把无关 formatting/docs 文件加入 semantic manifest。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import hashlib
from pathlib import Path

EPISODIC_IMPL_REF_KIND = "agentevalops_episodic_evaluation_implementation_ref"

#: Episodic evaluation semantic source manifest（relative to backend root，stable order）。
EPISODIC_SEMANTIC_SOURCE_FILES: tuple[str, ...] = (
    "app/core/evaluation/episodic_dataset.py",
    "app/core/evaluation/episodic_assertion.py",
    "app/core/evaluation/episodic_evidence.py",
    "app/core/evaluation/episodic_identity.py",
    "app/core/evaluation/episodic_step_identity.py",
    "app/core/evaluation/episodic_projection.py",
    "app/core/evaluation/episodic_evaluators.py",
    "app/core/evaluation/episodic_metrics.py",
    "app/core/evaluation/episodic_gate.py",
    "app/core/evaluation/episodic_artifact.py",
    "app/core/evaluation/episodic_impl_ref.py",
    "app/core/evaluation/episodic_baseline.py",
    "app/core/evaluation/stateful_journal.py",
    "app/core/evaluation/stateful_projection.py",
    "app/core/evaluation/execution.py",
    "app/core/evaluation/run_attempts.py",
    "app/services/evaluation/episodic_environment.py",
    "app/services/evaluation/episodic_runner.py",
    "app/adapters/evaluation/episodic_http_target.py",
    "app/adapters/evaluation/http_localagent.py",
    "app/registry/settings.py",
    "evaluation_assets/stateful_episodic_v1/stateful_episodic_dataset.v1.json",
    "evaluation_assets/stateful_episodic_v2/stateful_episodic_dataset.v2.json",
)


def _default_backend_root() -> Path:
    return Path(__file__).resolve().parents[3]


def episodic_evaluation_implementation_ref(
    *,
    backend_root: Path | None = None,
) -> str:
    """计算 ``sha256:<hex>`` 的 AgentEvalOps episodic implementation ref。

    Args:
        backend_root: backend 根目录（默认取本模块所在 backend）。

    Returns:
        ``sha256:<content_digest>``。

    Raises:
        FileNotFoundError: manifest 中任一 semantic 源文件缺失。
    """
    root = backend_root or _default_backend_root()
    digest = hashlib.sha256()
    for relative in EPISODIC_SEMANTIC_SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"episodic semantic source file missing: {path}")
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


__all__ = [
    "EPISODIC_IMPL_REF_KIND",
    "EPISODIC_SEMANTIC_SOURCE_FILES",
    "episodic_evaluation_implementation_ref",
]
