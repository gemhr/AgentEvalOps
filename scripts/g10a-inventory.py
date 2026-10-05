"""离线提取 G10A 源码清单；不导入应用、不读取环境或连接数据库。"""

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "go-backend/dist/g10a-evidence"
OUT.mkdir(parents=True, exist_ok=True)


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


endpoints = []
for path in sorted((ROOT / "backend/app/api/v1/routes").glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prefixes = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "APIRouter":
            prefixes[node.targets[0].id] = next((ast.literal_eval(k.value) for k in node.value.keywords if k.arg == "prefix"), "")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute) or not isinstance(d.func.value, ast.Name) or d.func.value.id not in prefixes:
                continue
            method = d.func.attr.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            route = "/v1" + prefixes[d.func.value.id] + ast.literal_eval(d.args[0])
            auth_writes = any(isinstance(c, ast.Name) and c.id in {"get_api_context", "get_data_plane_context", "require_project"} for c in ast.walk(node.args))
            category = "READ_ONLY_LEGACY_ALLOWED" if method == "GET" and not auth_writes else "CUTOVER_REQUIRED"
            endpoints.append({"method": method, "endpoint": route, "classification": category, "authentication_may_write": auth_writes, "consumer_status": "NOT_VERIFIED", "source": path.relative_to(ROOT).as_posix(), "line": d.lineno, "entry": node.name})
if len(endpoints) != 95:
    raise RuntimeError(f"router inventory drift: expected 95, got {len(endpoints)}")
write("legacy-endpoints.json", endpoints)

writers = []
mutation_names = {"add", "add_all", "delete", "flush", "commit", "execute", "bulk_save_objects", "incr", "incrby", "decr", "set", "setex", "hset", "rpush", "lpush", "delay", "apply_async", "send_task"}
for directory in ("backend/app/api", "backend/app/services", "backend/app/infrastructure", "backend/scripts"):
    for path in sorted((ROOT / directory).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = sorted({c.func.attr for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in mutation_names})
            if calls:
                writers.append({"source": path.relative_to(ROOT).as_posix(), "line": node.lineno, "symbol": node.name, "mutation_or_enqueue_calls": calls, "classification": "OFFLINE_ONLY" if directory == "backend/scripts" else "REQUIRES_CALL_CHAIN_REVIEW"})
write("python-mutation-candidates.json", writers)

consumers = [
    ("frontend", "frontend deployment operator", "legacy /v1 + Go project paths", "build-time Go Product API; legacy identity/settings decisions required"),
    ("evalgate-ci", "CI operator", "Python scripts/ci/release_gate.py", "Go evalgate explicit auth/runtime role"),
    ("LocalAgent", "Local_Agent deployment operator", "/integrations/localagent/v1/trace-envelopes", "/api/v1/projects/{project_id}/trace-envelopes"),
    ("sdk-cli-custom", "external integration owner required", "legacy /v1 + CLI exchange", "verified Go endpoint or explicit retirement"),
    ("celery-beat", "runtime operator", "Redis + Python tasks", "drain; revoke role; stop all schedules/workers"),
    ("billing-webhooks", "billing operator", "/v1/webhooks/stripe + schedules", "external control-plane decision required"),
    ("offline-migrations", "DB operator", "backend Alembic", "OFFLINE_ONLY with operator role"),
    ("UNKNOWN_EXTERNAL_CONSUMER", "UNCONFIRMED", "unknown", "BLOCKED"),
]
manifest = [{"consumer": name, "owner": owner, "current_endpoint": current, "target_endpoint": target, "cutover_status": "DECLARED", "rollback_requirement": "restore approved backup or restart Go; never resume stale Python writer", "verified_at": "", "verification_kind": "REAL_WORLD"} for name, owner, current, target in consumers]
write("consumer-manifest.declared.json", manifest)
digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.glob("*.json")) if p.name != "inventory-sha256.json"}
write("inventory-sha256.json", digests)
print(f"UTF-8 offline inventory: {len(endpoints)} endpoints, {len(writers)} mutation candidates, {len(manifest)} DECLARED consumers")
