"""WP5 Evaluation implementation provenance ref。

``evaluation_implementation_ref`` 绑定：

- repository HEAD（``git rev-parse HEAD``；失败时回退 ``unknown``）；
- 与 Stateful Evaluation 评分语义直接相关的 source 文件集合的稳定 content digest
  （sha256，stable path ordering，覆盖 source bytes）。

同一 source → 同一 ref；任一 semantic 源文件变化（即使 worktree dirty、HEAD 相同）
→ 不同 ref。用于 baseline numeric comparison 的兼容性判断与 artifact provenance。
"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

EVALUATION_IMPL_REF_KIND = "evaluation_implementation_ref"

# 与评分语义直接相关的 source 集合（相对 backend root，stable order）。
# 不把整个 repository 所有无关文件 hash 进去。
SEMANTIC_SOURCE_FILES: tuple[str, ...] = (
    "app/services/evaluation/stateful_runner.py",
    "app/services/evaluation/stateful_environment.py",
    "app/adapters/evaluation/http_localagent.py",
    "app/core/evaluation/stateful_journal.py",
    "app/core/evaluation/stateful_evaluators.py",
    "app/core/evaluation/stateful_metrics.py",
    "app/core/evaluation/stateful_gate.py",
    "app/core/evaluation/stateful_projection.py",
    "app/core/evaluation/stateful_assertion.py",
    "app/core/evaluation/stateful_artifact.py",
    "app/core/evaluation/stateful_baseline.py",
    "app/core/evaluation/stateful_memory_dataset.py",
    "app/core/evaluation/stateful_memory_dataset_v2.py",
    "app/registry/settings.py",
)


def _default_backend_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _repo_head(backend_root: Path) -> str:
    repo_root = backend_root.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def _content_digest(backend_root: Path) -> str:
    digest = hashlib.sha256()
    for rel in sorted(SEMANTIC_SOURCE_FILES):
        path = backend_root / rel
        if not path.is_file():
            raise FileNotFoundError(f"semantic source file missing: {path}")
        data = path.read_bytes()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
    return digest.hexdigest()


def evaluation_implementation_ref(
    *,
    backend_root: Path | None = None,
    head: str | None = None,
) -> str:
    """计算 evaluation implementation ref：``<head>:sha256:<content_digest>``。

    Args:
        backend_root: backend 根目录（默认取本模块所在 backend）。
        head: 显式 repository HEAD（默认运行 git rev-parse；失败回退 unknown）。

    Returns:
        ``f"{head}:sha256:{digest}"``。
    """
    root = backend_root or _default_backend_root()
    resolved_head = head if head is not None else _repo_head(root)
    return f"{resolved_head}:sha256:{_content_digest(root)}"


__all__ = [
    "EVALUATION_IMPL_REF_KIND",
    "SEMANTIC_SOURCE_FILES",
    "evaluation_implementation_ref",
]
