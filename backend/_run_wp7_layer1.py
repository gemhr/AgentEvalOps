"""WP7-E deterministic v4 runner; all facts come from LocalAgent HTTP responses."""
from __future__ import annotations
import asyncio, hashlib, json, uuid
from pathlib import Path
import httpx
from app.services.evaluation.episodic_environment import EpisodicLocalAgentProvisioner

ROOT=Path(r"D:\PythonProject\AgentEvalOps")
TARGET=Path(r"D:\PythonProject\Local_Agent")
DATA=ROOT/'backend/evaluation_assets/multi_agent_memory_governance_v1/dataset.json'
OUT=ROOT/'backend/evaluation_artifacts/multi_agent_memory_governance'
PY=TARGET/'.venv/Scripts/python.exe'

def control(kind):
    base={"requester_agent_id":"agent-a"}
    private={"fixture_ref":"private-a","owner_agent_id":"agent-a","logical_key":"database","canonical_text":"SQLite deployment"}
    project={"project_identity":{"project_id":"project-p"},"project_grants":[{"project_id":"project-p","agent_id":"agent-a","permissions":["READ","WRITE","PROMOTE"]}]}
    if kind=="PRIVATE_OWNER_READ": base.update(private_fixtures=[private],operation={"operation":"PRIVATE_READ","target_owner_agent_id":"agent-a"})
    elif kind=="FOREIGN_PRIVATE_LEAKAGE": base.update(requester_agent_id="agent-b",private_fixtures=[private],operation={"operation":"PRIVATE_READ","target_owner_agent_id":"agent-a"})
    elif kind=="FOREIGN_PRIVATE_MUTATION": base.update(requester_agent_id="agent-b",private_fixtures=[private],operation={"operation":"PRIVATE_FORGET","target_owner_agent_id":"agent-a","logical_key":"database"})
    elif kind=="PROJECT_SHARED_RECALL": base.update(**project,operation={"operation":"PROJECT_WRITE","logical_key":"database","canonical_text":"PostgreSQL"})
    elif kind in {"PROJECT_SCOPE_ISOLATION","MISSING_PROJECT_GRANT"}: base.update(project_identity={"project_id":"project-q"} if kind=="PROJECT_SCOPE_ISOLATION" else {"project_id":"project-p"},operation={"operation":"PROJECT_READ"})
    elif kind=="UNAUTHORIZED_PROJECT_MUTATION": base.update(project_identity={"project_id":"project-p"},project_grants=[{"project_id":"project-p","agent_id":"agent-a","permissions":["READ"]}],operation={"operation":"PROJECT_FORGET","logical_key":"database"})
    elif kind=="PRIVATE_TO_PROJECT_PROMOTION": base.update(**project,private_fixtures=[private],operation={"operation":"PRIVATE_TO_PROJECT_PROMOTION","target_owner_agent_id":"agent-a","source_memory_id":"wp7-fixture-"+uuid.uuid5(uuid.NAMESPACE_URL,"private-a").hex})
    elif kind=="UNAUTHORIZED_PROMOTION": base.update(requester_agent_id="agent-b",project_identity={"project_id":"project-p"},project_grants=[{"project_id":"project-p","agent_id":"agent-b","permissions":["WRITE","PROMOTE"]}],private_fixtures=[private],operation={"operation":"PRIVATE_TO_PROJECT_PROMOTION","target_owner_agent_id":"agent-a","source_memory_id":"wp7-fixture-"+uuid.uuid5(uuid.NAMESPACE_URL,"private-a").hex})
    elif kind in {"SPECIALIST_STEP_OWNERSHIP","DELEGATION_SYNTHESIS_BOUNDARY"}: base.update(requester_agent_id="core_router",deterministic_multi_agent=True)
    else: base.update(**project,operation={"operation":"PROJECT_READ"})
    return base

def passed(kind,b):
    p=b['private_retrieval']; q=b['project_retrieval']; a=b.get('authorization') or {}; m=b.get('mutation') or {}; x=b.get('promotion') or {}
    if kind=="PRIVATE_OWNER_READ": return a.get('decision')=='ALLOW' and p['selected_count']>0 and p['injected_count']>0
    if kind=="FOREIGN_PRIVATE_LEAKAGE": return a.get('decision')=='DENY' and not any(p[k] for k in ('candidate_count','selected_count','injected_count'))
    if kind in {'FOREIGN_PRIVATE_MUTATION','UNAUTHORIZED_PROJECT_MUTATION'}: return a.get('decision')=='DENY' and m.get('affected_count')==0
    if kind=='PRIVATE_TO_PROJECT_PROMOTION': return x.get('decision')=='ALLOW' and x.get('provenance_complete')
    if kind=='UNAUTHORIZED_PROMOTION': return x.get('decision')=='DENY' and not x.get('resulting_project_memory_ref')
    if kind=='SPECIALIST_STEP_OWNERSHIP': return bool(b['specialist_formation']) and all(i['verified_performer']==i['episode_owner'] for i in b['specialist_formation'])
    if kind=='DELEGATION_SYNTHESIS_BOUNDARY': return all(not i['private_bundle_present'] for i in b['invocation_visibility'])
    if kind=='MEMORY_TRUST_BOUNDARY': return all(i.get('trust_role')=='user_content' for i in q.get('context_sources',[]))
    return q['selected_count']==0 if kind in {'PROJECT_SCOPE_ISOLATION','MISSING_PROJECT_GRANT'} else (a.get('decision')=='ALLOW' or m.get('outcome') in {'CREATED','SUPERSEDED'})

async def main():
 d=json.loads(DATA.read_text()); results=[]
 prov=EpisodicLocalAgentProvisioner(localagent_repo=TARGET,base_work_dir=OUT/'runs',localagent_python_executable=PY,health_timeout_seconds=90)
 for s in d['scenarios']:
  env=await prov.provision(type('S',(),{'scenario_id':s['id']})())
  try:
   payload={'agent_id':'core_router','query':'database deployment','run_id':str(uuid.uuid4()),'timeout_seconds':60,'evaluation_control':control(s['kind'])}
   async with httpx.AsyncClient(timeout=75,trust_env=False) as c: r=await c.post(env.localagent_base_url+'/api/runtime/evaluation-execute/v4',json=payload); b=r.json()
   results.append({'scenario_id':s['id'],'kind':s['kind'],'http_status':r.status_code,'pass':r.status_code==200 and passed(s['kind'],b),'evidence':b})
  finally: await prov.cleanup(env,preserve=True)
 artifact={'schema_version':'multi-agent-memory-governance-experiment.v1','dataset_id':d['dataset_id'],'dataset_digest':'sha256:'+hashlib.sha256(DATA.read_bytes()).hexdigest(),'target_implementation_ref':'sha256:107ff45eace28849162ddfda1bdfda2bb5e064eee50f36cd0fd0d8b6434b46d0','execution_policy':'GLOBAL_SEQUENTIAL','real_model_experiment_executed':False,'scenarios':results,'gate':'PASS' if all(x['pass'] for x in results) else 'FAIL'}
 OUT.mkdir(parents=True,exist_ok=True); path=OUT/'experiment_artifact.v1.json'; path.write_text(json.dumps(artifact,ensure_ascii=False,indent=2)); print(path); print(json.dumps({'pass':sum(x['pass'] for x in results),'fail':sum(not x['pass'] for x in results),'gate':artifact['gate']}))
asyncio.run(main())
