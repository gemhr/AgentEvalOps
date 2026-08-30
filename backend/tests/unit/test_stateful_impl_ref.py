"""Evaluation implementation ref（E1-R2 / I）：HEAD + stable content digest。"""

# ruff: noqa: D101, D105, D415

from pathlib import Path

from app.core.evaluation.stateful_impl_ref import (
    SEMANTIC_SOURCE_FILES,
    evaluation_implementation_ref,
)


def _make_root(tmp_path: Path, mutate: str | None = None) -> Path:
    root = tmp_path / "backend"
    for rel in SEMANTIC_SOURCE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "SOURCE_FOR_REF_TEST\n"
        if mutate and rel == mutate:
            content += "MUTATED\n"
        path.write_text(content, encoding="utf-8")
    return root


def test_same_files_same_ref():
    root = _make_root(Path("C:/tmp/x"))
    ref1 = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    ref2 = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    assert ref1 == ref2
    assert ref1.startswith("deadbeef:sha256:")


def test_change_evaluator_source_changes_ref():
    root = _make_root(Path("C:/tmp/x"))
    ref_base = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    mutated = _make_root(Path("C:/tmp/x"), mutate="app/core/evaluation/stateful_evaluators.py")
    ref_mutated = evaluation_implementation_ref(backend_root=mutated, head="deadbeef")
    assert ref_base != ref_mutated


def test_change_settle_settings_source_changes_ref():
    root = _make_root(Path("C:/tmp/x"))
    ref_base = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    mutated = _make_root(Path("C:/tmp/x"), mutate="app/registry/settings.py")
    ref_mutated = evaluation_implementation_ref(backend_root=mutated, head="deadbeef")
    assert ref_base != ref_mutated


def test_same_head_different_dirty_source_changes_ref():
    root = _make_root(Path("C:/tmp/x"))
    ref_clean = evaluation_implementation_ref(backend_root=root, head="HEAD")
    dirty = _make_root(Path("C:/tmp/x"), mutate="app/services/evaluation/stateful_runner.py")
    ref_dirty = evaluation_implementation_ref(backend_root=dirty, head="HEAD")
    assert ref_clean != ref_dirty


def test_v2_dataset_semantic_source_is_part_of_ref():
    assert "app/core/evaluation/stateful_memory_dataset_v2.py" in SEMANTIC_SOURCE_FILES
    root = _make_root(Path("C:/tmp/x"))
    ref_base = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    mutated = _make_root(Path("C:/tmp/x"), mutate="app/core/evaluation/stateful_memory_dataset_v2.py")
    ref_mutated = evaluation_implementation_ref(backend_root=mutated, head="deadbeef")
    # pre-R3 vs post-R3：新增 V2 schema/evidence semantics source 必须改变 implementation ref
    assert ref_base != ref_mutated


def test_http_localagent_transport_source_is_part_of_ref():
    assert "app/adapters/evaluation/http_localagent.py" in SEMANTIC_SOURCE_FILES
    root = _make_root(Path("C:/tmp/x"))
    ref_base = evaluation_implementation_ref(backend_root=root, head="deadbeef")
    mutated = _make_root(Path("C:/tmp/x"), mutate="app/adapters/evaluation/http_localagent.py")
    ref_mutated = evaluation_implementation_ref(backend_root=mutated, head="deadbeef")
    assert ref_base != ref_mutated


def test_different_head_same_source_differs():
    root = _make_root(Path("C:/tmp/x"))
    ref_a = evaluation_implementation_ref(backend_root=root, head="aaaa")
    ref_b = evaluation_implementation_ref(backend_root=root, head="bbbb")
    assert ref_a != ref_b


def test_ref_is_stable_and_deterministic():
    root = _make_root(Path("C:/tmp/x"))
    ref = evaluation_implementation_ref(backend_root=root, head="HEAD")
    assert len(ref) > 20
    # format: <head>:sha256:<hexdigest>
    assert ref.count(":") == 2
    assert "sha256" in ref
