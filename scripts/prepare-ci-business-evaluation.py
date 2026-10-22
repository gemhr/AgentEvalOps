"""将用户已审核并授权分享的小型模拟事实 bundle 发布到现有项目目录。"""
import argparse
import json
import os
from pathlib import Path
import uuid
from urllib.request import Request, build_opener, ProxyHandler

METRICS = ["ci_record_consistency.v1", "ci_candidate_relation_agreement.v1", "ci_unsafe_association.v1", "ci_repair_state_consistency.v1"]


def publish(args):
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    if bundle.get("status") != "USER_APPROVED" or bundle.get("project_id") != args.project or len(bundle.get("cases", [])) != 8:
        raise ValueError("必须使用用户已确认分享至此项目的八例 bundle")
    for case in bundle["cases"]:
        reference = case["ground_truth"]
        if reference["label_status"] not in {"VERIFIED_SIMULATION", "REVIEWED_OBSERVATION"} or not reference["reviewer"] or not reference["reviewed_at"]:
            raise ValueError("未审核参考不能作为正式八例发布")
    journal_path = Path(args.output)
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {"project_id": args.project, "commands": {}, "assets": {}}
    opener = build_opener(ProxyHandler({}))
    def save():
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    def post(key, path, body):
        command = journal["commands"].setdefault(key, str(uuid.uuid4()))
        save()
        request = Request(args.base_url.rstrip("/") + "/api/v1/projects/" + args.project + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers={
                "Authorization": "Bearer " + os.environ[args.token_env], "Content-Type": "application/json", "Idempotency-Key": command})
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    def asset(kind, name, body):
        key = kind + ":" + name
        if key in journal["assets"]:
            return journal["assets"][key]
        logical = post(key + ":create", "/" + kind, {"name": name})
        version = post(key + ":publish", f"/{kind}/{logical['id']}/versions", {"version": "v1", "body": body})
        journal["assets"][key] = version
        save()
        return version
    app = dict(case_types=["REGRESSION"], required_evidence=[], rule_ref="ci-business-applicability.v1")
    cases = []
    for case in bundle["cases"]:
        value = asset("cases", "CI Business " + case["name"], dict(input=case["input"], ground_truth=case["ground_truth"],
            task_goal="检查冻结的模拟 CI 业务观察", acceptance_criteria=["按独立审核参考核对业务合同；未知不伪装成功"], expected_output=None,
            assertions=[], applicability=app, case_type="REGRESSION", capability="CI_BUSINESS_QUALITY", criticality="NORMAL",
            tags=[case["name"], "SIMULATED_BUSINESS_RULE_QUALITY"], metadata=case["metadata"], body_policy="RETAINED"))
        cases.append(value["ref"])
    dataset = asset("datasets", "CI Business Quality 8", dict(ordered_cases=cases, metadata=dict(
        purpose="SIMULATED_BUSINESS_RULE_QUALITY", shared_project_id=args.project, approval=bundle["approval"],
        composition=[c["name"] for c in bundle["cases"]], label_counts={"VERIFIED_SIMULATION": 7, "REVIEWED_OBSERVATION": 1},
        source_exports=[{k: e[k] for k in ("export_digest", "exported_at", "exported_by_user_id", "source_deployment", "trust")} for e in bundle["exports"].values()])) )
    metrics, evaluators = [], []
    for name in METRICS:
        metric = asset("metrics", name, dict(name=name, grain="CASE", value_type="BOOLEAN", labels=[], score_range=[0, 1], unit="flag",
            direction="LOWER_IS_BETTER" if name == METRICS[2] else "HIGHER_IS_BETTER", denominator_semantics="reviewed applicable business cases",
            missing_semantics="INCONCLUSIVE/null; missing evidence never passes", aggregation_semantics="MEAN", comparison_semantics="paired frozen cases",
            applicability=app, availability="CONTRACT_ONLY", parameters={}))
        evaluator = asset("evaluators", name, dict(kind="DETERMINISTIC", input_contract="ci-business-observations.v1", output_metrics=[metric["ref"]],
            applicability=app, availability="CONTRACT_ONLY", implementation_ref="ci-business-deterministic.v1", config_snapshot=dict(metric=name), rubric=None,
            prompt_ref=None, schema_version="ci-business-quality-output.v1", model_binding=None,
            execution_budget=dict(total_milliseconds=10000, max_provider_calls=0, max_response_bytes=4194304),
            retry_policy=dict(max_evaluation_attempts=1, initial_backoff_milliseconds=0, max_backoff_milliseconds=0), normalization_semantics="ci-business-null-preserving.v1"))
        metrics.append(metric["ref"])
        evaluators.append(dict(evaluator=evaluator["ref"], metrics=[metric["ref"]], required=True, applicability=app))
    policy = asset("policies", "CI Business Quality Gate", dict(Offline=True, Online=False, Criticalities=["NORMAL"], CriticalMissing=True, BlockPerCase=True,
        Coverage=dict(MinimumDecision=1, MinimumEvaluation=1, MaximumMissing=0, MaximumUnknown=0, MaximumError=0, MaximumUnsupported=0, DropTolerance=0),
        TaskSuccess=None, Metrics=[dict(Metric=ref, Aggregation="mean", Minimum=None, Maximum=0 if i == 2 else None,
            AbsoluteTolerance=0, RelativeTolerance=0, MinimumCoverage=1, Required=True, PerCase=True, TargetRange=None) for i, ref in enumerate(metrics)],
        AcceptedDifferences=[], Exceptions=[]))
    suite = asset("suites", "CI Business Quality Suite", dict(dataset_version=dataset["ref"], explicit_cases=[], evaluator_bindings=evaluators,
        metric_versions=metrics, policy_version=dict(policy_id=policy["ref"]["entity_id"], version=policy["ref"]["version"],
            digest=policy["content_digest"], algorithm_ref=policy["algorithm_ref"]), applicability=app, metadata=dict(purpose="SIMULATED_BUSINESS_RULE_QUALITY")))
    journal.update(dataset=dataset["ref"], suite=suite["ref"], policy=policy["ref"], metrics=metrics)
    save()
    return {k: journal[k] for k in ("project_id", "dataset", "suite", "policy")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--output", required=True)
    print(json.dumps(publish(parser.parse_args()), ensure_ascii=False))
