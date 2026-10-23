"""发布获授权的开发公开输入与独立覆盖参考，复用现有 Catalog/Suite/Run API。"""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def publish(args):
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    if bundle.get("purpose") != "DEVELOPMENT_EXPOSED" or not bundle.get("reference_review_ref"):
        raise ValueError("需要明确的开发参考审核依据；本脚本不处理最终 Holdout")
    inp, truth = bundle["input"], bundle["coverage_truth"]
    if truth["input_digest"] != inp["snapshot"]["input_digest"] or truth["corpus_digest"] != inp["snapshot"]["corpus_digest"]:
        raise ValueError("冻结输入与独立参考不匹配")
    opener = build_opener(ProxyHandler({}))
    def post(path, body):
        request = Request(args.base_url.rstrip("/") + "/api/v1/projects/" + args.project + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers={"Authorization": "Bearer " + os.environ[args.token_env], "Content-Type": "application/json", "Idempotency-Key": str(uuid4())})
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    def asset(kind, name, body):
        logical = post("/" + kind, dict(name=name))
        return post(f"/{kind}/{logical['id']}/versions", dict(version="v1", body=body))
    app = dict(case_types=["REGRESSION"], required_evidence=[], rule_ref="feature-impact-public.v1")
    case = asset("cases", "历史特性影响开发样本", dict(input=inp, ground_truth=truth, task_goal="分析冻结公开技术资料并提出覆盖建议", acceptance_criteria=["基于真实证据区分共享风险、隔离反证和待澄清项"], expected_output=None, assertions=[], applicability=app, case_type="REGRESSION", capability="FEATURE_IMPACT", criticality="NORMAL", tags=["DEVELOPMENT_EXPOSED"], metadata=dict(reference_review_ref=bundle["reference_review_ref"]), body_policy="RETAINED"))
    dataset = asset("datasets", "历史影响开发集合", dict(ordered_cases=[case["ref"]], metadata=dict(purpose="DEVELOPMENT_EXPOSED")))
    metric = asset("metrics", "feature-impact-quality.v1", dict(name="feature-impact-quality.v1", grain="CASE", value_type="ENUM", labels=["FAIL", "INCONCLUSIVE"], score_range=None, unit="counts", direction="NONE", denominator_semantics="frozen coverage units", missing_semantics="null/pending", aggregation_semantics="NONE", comparison_semantics="same frozen task/corpus/truth", applicability=app, availability="CONTRACT_ONLY", parameters={}))
    evaluator = asset("evaluators", "历史影响独立判分", dict(kind="DETERMINISTIC", input_contract="feature-impact-input.v1", output_metrics=[metric["ref"]], applicability=app, availability="CONTRACT_ONLY", implementation_ref="feature-impact-deterministic.v1", config_snapshot=dict(metric="feature-impact-quality.v1"), rubric=None, prompt_ref=None, schema_version="feature-impact-output.v1", model_binding=None, execution_budget=dict(total_milliseconds=10000, max_provider_calls=0, max_response_bytes=1048576), retry_policy=dict(max_evaluation_attempts=1, initial_backoff_milliseconds=0, max_backoff_milliseconds=0), normalization_semantics="feature-impact-counts.v1"))
    # 既有 Gate 对 pending/缺证据保持阻断；不设置未经授权的质量百分比门槛。
    policy = asset("policies", "历史影响待人审门禁", dict(Offline=True, Online=False, Criticalities=["NORMAL"], CriticalMissing=True, BlockPerCase=True, Coverage=dict(MinimumDecision=1, MinimumEvaluation=1, MaximumMissing=0, MaximumUnknown=0, MaximumError=0, MaximumUnsupported=0, DropTolerance=0), TaskSuccess=None, Metrics=[dict(Metric=metric["ref"], Aggregation="coverage", Minimum=None, Maximum=None, AbsoluteTolerance=None, RelativeTolerance=None, MinimumCoverage=1, Required=True, PerCase=True, TargetRange=None)], AcceptedDifferences=[], Exceptions=[]))
    suite = asset("suites", "历史影响业务评测", dict(dataset_version=dataset["ref"], explicit_cases=[], evaluator_bindings=[dict(evaluator=evaluator["ref"], metrics=[metric["ref"]], required=True, applicability=app)], metric_versions=[metric["ref"]], policy_version=dict(policy_id=policy["ref"]["entity_id"], version=policy["ref"]["version"], digest=policy["content_digest"], algorithm_ref=policy["algorithm_ref"]), applicability=app, metadata=dict(purpose="DEVELOPMENT_EXPOSED")))
    result = dict(project_id=args.project, dataset=dataset["ref"], suite=suite["ref"], policy=policy["ref"])
    if args.run:
        result["run"] = post("/feature-impact/runs", dict(suite=suite["ref"]))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("bundle", "project", "base-url", "token-env", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--run", action="store_true")
    print(json.dumps(publish(parser.parse_args()), ensure_ascii=False))
