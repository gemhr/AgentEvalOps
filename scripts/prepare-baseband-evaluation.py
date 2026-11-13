"""发布公开开发样本及冻结评分合同；selector 独立于 Case 业务输入。"""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def publish(args):
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    if bundle["version"] != "baseband-development.v1" or bundle["evaluator_version"] != "baseband-deterministic.v1":
        raise ValueError("BASEBAND_DEVELOPMENT_CONTRACT_INVALID")
    selectors = json.loads(Path(args.selectors).read_text(encoding="utf-8"))
    opener = build_opener(ProxyHandler({}))
    def post(path, body):
        request = Request(args.base_url.rstrip("/") + "/api/v1/projects/" + args.project + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers={"Authorization": "Bearer " + os.environ[args.token_env], "Content-Type": "application/json", "Idempotency-Key": str(uuid4())})
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    def asset(kind, name, body, version="v1"):
        logical = post("/" + kind, dict(name=name))
        return post(f"/{kind}/{logical['id']}/versions", dict(version=version, body=body))
    app = dict(case_types=["REGRESSION"], required_evidence=[], rule_ref="baseband-public.v1")
    cases, bindings = [], {}
    for sample in bundle["cases"]:
        truth = sample["ground_truth"]
        if truth["visibility"] != "DEVELOPMENT_VISIBLE":
            raise ValueError("PUBLIC_DEVELOPMENT_ONLY")
        case = asset("cases", sample["case_key"], dict(input=sample["input"], ground_truth=truth,
            task_goal="依据已观察证据调查基带输入与实际执行", acceptance_criteria=["区分配置机制、正常对照和证据不足"],
            expected_output=None, assertions=[], applicability=app, case_type="REGRESSION", capability="BASEBAND", criticality="NORMAL",
            tags=["DEVELOPMENT_VISIBLE"], metadata=dict(reference_review_ref="baseband-truth.v1"), body_policy="RETAINED"))
        cases.append(case["ref"]); bindings[case["ref"]["entity_id"]] = selectors[sample["case_key"]]
    dataset = asset("datasets", "基带公开开发集合", dict(ordered_cases=cases, metadata=dict(purpose="DEVELOPMENT_VISIBLE")))
    numeric = getattr(args, "version", "v1") == "v2"
    metric_name = "baseband_diagnosis_correctness.v2" if numeric else "baseband_diagnosis.v1"
    metric = asset("metrics", metric_name, dict(name=metric_name, grain="CASE", value_type="SCALAR" if numeric else "ENUM", labels=[] if numeric else ["PASS", "FAIL", "INCONCLUSIVE"], score_range=[0,1] if numeric else None, unit="diagnosis", direction="HIGHER_IS_BETTER" if numeric else "NONE", denominator_semantics="frozen public cases", missing_semantics="null/inconclusive", aggregation_semantics="mean" if numeric else "NONE", comparison_semantics="same semantic input/truth/evaluator", applicability=app, availability="CONTRACT_ONLY", parameters={}), "v2" if numeric else "v1")
    evaluator = asset("evaluators", "基带独立确定性判分", dict(kind="DETERMINISTIC", input_contract="baseband-input.v1", output_metrics=[metric["ref"]], applicability=app, availability="CONTRACT_ONLY", implementation_ref="baseband-deterministic.v2" if numeric else "baseband-deterministic.v1", config_snapshot=dict(metric=metric_name), rubric=None, prompt_ref=None, schema_version="baseband-quality-output.v1", model_binding=None, execution_budget=dict(total_milliseconds=10000, max_provider_calls=0, max_response_bytes=1048576), retry_policy=dict(max_evaluation_attempts=1, initial_backoff_milliseconds=0, max_backoff_milliseconds=0), normalization_semantics="baseband-correctness-null-preserving.v2" if numeric else "baseband-null-preserving.v1"), "v2" if numeric else "v1")
    policy = asset("policies", "基带开发比较", dict(Offline=True, Online=False, Criticalities=["NORMAL"], CriticalMissing=True, BlockPerCase=True, Coverage=dict(MinimumDecision=1, MinimumEvaluation=1, MaximumMissing=0, MaximumUnknown=0, MaximumError=0, MaximumUnsupported=0, DropTolerance=0), TaskSuccess=None, Metrics=[dict(Metric=metric["ref"], Aggregation="mean" if numeric else "coverage", Minimum=1 if numeric else None, Maximum=None, AbsoluteTolerance=0 if numeric else None, RelativeTolerance=None, MinimumCoverage=1, Required=True, PerCase=True, TargetRange=None)], AcceptedDifferences=[], Exceptions=[]), "v2" if numeric else "v1")
    suite = asset("suites", "基带业务评测", dict(dataset_version=dataset["ref"], explicit_cases=[], evaluator_bindings=[dict(evaluator=evaluator["ref"], metrics=[metric["ref"]], required=True, applicability=app)], metric_versions=[metric["ref"]], policy_version=dict(policy_id=policy["ref"]["entity_id"], version=policy["ref"]["version"], digest=policy["content_digest"], algorithm_ref=policy["algorithm_ref"]), applicability=app, metadata=dict(purpose="DEVELOPMENT_VISIBLE")), "v2" if numeric else "v1")
    result = dict(project_id=args.project, dataset=dataset["ref"], suite=suite["ref"], policy=policy["ref"], selectors=bindings)
    result["run"] = post("/baseband/runs", dict(suite=suite["ref"], selectors=bindings))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("bundle", "selectors", "project", "base-url", "token-env", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--version", choices=["v1","v2"], default="v1")
    print(json.dumps(publish(parser.parse_args()), ensure_ascii=False))
