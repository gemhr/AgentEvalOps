"""发布独立覆盖工作流公开 Case/GT 与冻结评价合同，不向 Target 传 GT。"""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from uuid import uuid4


def definitions():
    app=dict(case_types=["REGRESSION"],required_evidence=[],rule_ref="coverage-execution-public.v1")
    metric=dict(name="coverage_execution_correctness.v1",grain="CASE",value_type="SCALAR",labels=[],score_range=[0,1],unit="workflow",direction="HIGHER_IS_BETTER",denominator_semantics="sealed workflow",missing_semantics="null/inconclusive",aggregation_semantics="mean",comparison_semantics="same frozen coverage contract",applicability=app,availability="CONTRACT_ONLY",parameters={})
    evaluator=dict(kind="DETERMINISTIC",input_contract="coverage-execution-input.v1",output_metrics=[],applicability=app,availability="CONTRACT_ONLY",implementation_ref="coverage-execution-deterministic.v1",config_snapshot=dict(metric=metric["name"]),rubric=None,prompt_ref=None,schema_version="coverage-execution-output.v1",model_binding=None,execution_budget=dict(total_milliseconds=10000,max_provider_calls=0,max_response_bytes=1048576),retry_policy=dict(max_evaluation_attempts=1,initial_backoff_milliseconds=0,max_backoff_milliseconds=0),normalization_semantics="coverage-execution-score.v1")
    policy=dict(Offline=True,Online=False,Criticalities=["NORMAL"],CriticalMissing=True,BlockPerCase=True,Coverage=dict(MinimumDecision=1,MinimumEvaluation=1,MaximumMissing=0,MaximumUnknown=0,MaximumError=0,MaximumUnsupported=0,DropTolerance=0),TaskSuccess=None,Metrics=[],AcceptedDifferences=[],Exceptions=[])
    return app,metric,evaluator,policy


def publish(a):
    bundle=json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    if bundle["version"]!="coverage-execution-development.v1":raise ValueError("COVERAGE_CONTRACT_INVALID")
    selectors=json.loads(Path(a.selectors).read_text(encoding="utf-8"));opener=build_opener(ProxyHandler({}))
    def post(path,body):
        req=Request(a.base_url.rstrip("/")+"/api/v1/projects/"+a.project+path,data=json.dumps(body,ensure_ascii=False).encode(),method="POST",headers={"Authorization":"Bearer "+os.environ[a.token_env],"Content-Type":"application/json","Idempotency-Key":str(uuid4())})
        with opener.open(req,timeout=30) as response:return json.load(response)
    def asset(kind,name,body):
        logical=post("/"+kind,dict(name=name))
        return post(f"/{kind}/{logical['id']}/versions",dict(version="v1",body=body))
    app,metric,evaluator,policy=definitions();cases=[];bindings={}
    for sample in bundle["cases"]:
        if sample["ground_truth"]["visibility"]!="DEVELOPMENT_VISIBLE":raise ValueError("PUBLIC_ONLY")
        case=asset("cases",sample["case_key"],dict(input=sample["input"],ground_truth=sample["ground_truth"],task_goal="审核覆盖计划、资产与人工执行来源",acceptance_criteria=["五项独立业务条件"],expected_output=None,assertions=[],applicability=app,case_type="REGRESSION",capability="COVERAGE_EXECUTION",criticality="NORMAL",tags=["DEVELOPMENT_VISIBLE"],metadata=dict(reference_review_ref="coverage-execution-truth.v1"),body_policy="RETAINED"))
        cases.append(case["ref"]);bindings[case["ref"]["entity_id"]]=selectors[sample["case_key"]]
    dataset=asset("datasets","覆盖工作流公开集合",dict(ordered_cases=cases,metadata=dict(purpose="DEVELOPMENT_VISIBLE")))
    m=asset("metrics",metric["name"],metric);evaluator["output_metrics"]=[m["ref"]];e=asset("evaluators","覆盖工作流独立评价",evaluator)
    policy["Metrics"]=[dict(Metric=m["ref"],Aggregation="mean",Minimum=1,Maximum=None,AbsoluteTolerance=0,RelativeTolerance=None,MinimumCoverage=1,Required=True,PerCase=True,TargetRange=None)]
    p=asset("policies","覆盖工作流绝对门槛",policy)
    suite=asset("suites","覆盖工作流评价",dict(dataset_version=dataset["ref"],explicit_cases=[],evaluator_bindings=[dict(evaluator=e["ref"],metrics=[m["ref"]],required=True,applicability=app)],metric_versions=[m["ref"]],policy_version=dict(policy_id=p["ref"]["entity_id"],version=p["ref"]["version"],digest=p["content_digest"],algorithm_ref=p["algorithm_ref"]),applicability=app,metadata=dict(purpose="DEVELOPMENT_VISIBLE")))
    result=dict(project_id=a.project,dataset=dataset["ref"],metric=m["ref"],evaluator=e["ref"],policy=p["ref"],suite=suite["ref"],selectors=bindings)
    result["run"]=post("/coverage-execution/runs",dict(suite=suite["ref"],selectors=bindings))
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("bundle","selectors","project","base-url","token-env","output"):parser.add_argument("--"+name,required=True)
    publish(parser.parse_args())
