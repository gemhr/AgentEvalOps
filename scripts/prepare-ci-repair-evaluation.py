"""通过 Go Product API 发布公开 TLV 样本、指标和严格 Gate 策略。"""

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def publish(args):
    bundle = json.loads((Path(__file__).resolve().parents[1] / "go-backend/testdata/ci_repair_development.json").read_text(encoding="utf-8"))
    if bundle["version"] != "ci-repair-development.v1":
        raise ValueError("CI_REPAIR_BUNDLE_INVALID")
    opener = build_opener(ProxyHandler({}))

    def post(path, body):
        request = Request(args.base_url.rstrip("/") + f"/api/v1/projects/{args.project}/{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers={
                "Authorization": "Bearer " + os.environ[args.token_env], "Content-Type": "application/json",
                "Idempotency-Key": str(uuid4()),
            })
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def asset(resource, name, body):
        item = post(resource, dict(name=name))
        return post(f"{resource}/{item['id']}/versions", dict(version="v1", body=body))

    app = dict(case_types=["REGRESSION"], required_evidence=[], rule_ref="ci-repair-development.v1")
    cases = [asset("cases", sample["case_id"], dict(input=dict(schema_version="ci-repair-case.v1", case_id=sample["case_id"]),
        ground_truth=sample["ground_truth"], task_goal="独立检查封存的 TLV 回放证据", acceptance_criteria=["核对 TLV 协议和业务值"],
        expected_output=None, assertions=[], applicability=app, case_type="REGRESSION", capability="CI_REPAIR",
        criticality="CRITICAL", tags=["DEVELOPMENT_VISIBLE"], metadata={}, body_policy="RETAINED"))["ref"] for sample in bundle["cases"]]
    visibility = dict(purpose="DEVELOPMENT_VISIBLE")
    dataset = asset("datasets", "TLV 修复公开开发集合", dict(ordered_cases=cases, metadata=visibility))
    metrics, bindings, rules = [], [], []
    for name in ("ci_repair.oracle_correctness.v1", "ci_repair.contract_validity.v1"):
        metric = asset("metrics", name, dict(name=name, grain="CASE", value_type="SCALAR", labels=[], score_range=[0, 1],
            unit="correctness", direction="HIGHER_IS_BETTER", denominator_semantics="two critical cases", missing_semantics="inconclusive",
            aggregation_semantics="mean", comparison_semantics="same frozen TLV cases", applicability=app, availability="CONTRACT_ONLY", parameters={}))
        mref = metric["ref"]
        metrics.append(mref)
        evaluator = asset("evaluators", name, dict(kind="DETERMINISTIC", input_contract="ci-repair-case.v1", output_metrics=[mref],
            applicability=app, availability="CONTRACT_ONLY", implementation_ref="ci-repair-oracle.v1", config_snapshot=dict(metric=name),
            rubric=None, prompt_ref=None, schema_version="ci-repair-evidence.v1", model_binding=None,
            execution_budget=dict(total_milliseconds=10000, max_provider_calls=0, max_response_bytes=1048576),
            retry_policy=dict(max_evaluation_attempts=1, initial_backoff_milliseconds=0, max_backoff_milliseconds=0),
            normalization_semantics="ci-repair-numeric.v1"))
        bindings.append(dict(evaluator=evaluator["ref"], metrics=[mref], required=True, applicability=app))
        rules.append(dict(Metric=mref, Aggregation="mean", Minimum=1, MinimumCoverage=1, Required=True, PerCase=True))
    policy = asset("policies", "TLV 修复发布条件", dict(Offline=True, Online=False, Criticalities=["CRITICAL"], CriticalMissing=True,
        BlockPerCase=True, Coverage=dict(MinimumDecision=1, MinimumEvaluation=1), TaskSuccess=None,
        Metrics=rules, AcceptedDifferences=[], Exceptions=[]))
    pref = policy["ref"]
    suite = asset("suites", "TLV 修复回放评测", dict(dataset_version=dataset["ref"], explicit_cases=[], evaluator_bindings=bindings,
        metric_versions=metrics, policy_version=dict(policy_id=pref["entity_id"], version=pref["version"],
            digest=policy["content_digest"], algorithm_ref=policy["algorithm_ref"]), applicability=app, metadata=visibility))
    return dict(project_id=args.project, dataset=dataset["ref"], suite=suite["ref"], policy=pref, cases=cases, metrics=metrics)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("project", "base-url", "token-env", "output"):
        parser.add_argument("--" + name, required=True)
    arguments = parser.parse_args()
    Path(arguments.output).write_text(json.dumps(publish(arguments), ensure_ascii=False, indent=2), encoding="utf-8")
