"use client";

import { useEffect, useState, useRef } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";

type Asset = { instance_id: string; asset_digest: string; state_revision: number; case_asset: Record<string, unknown>; workflow_source: Record<string, unknown>; distribution: { mode: string; target_ref: string; distribution_id: string }; execution_status: string; execution_intent: { case_id: string; expected_state_revision: number }; executions: { execution_id: string; incident_id: string | null; outcome: string }[] };
type Plan = { plan_id: string; plan_digest: string; tests: { test_id: string; test_kind?: string }[] };

export default function CoverageAssets({ plan, credential, onExecuted }: { plan: Plan; credential: string; onExecuted: () => Promise<void> }) {
  const [assets, setAssets] = useState<Asset[]>([]); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const intents = useRef<Record<string, string>>({});
  const base = `/api/agentcore/feature-impact/coverage-plans/${plan.plan_id}/case-assets`;
  async function api<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(path, { method: body ? "POST" : "GET", cache: "no-store", headers: { Authorization: `Bearer ${credential}`, ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
    if (!response.ok) throw new Error(`请求失败 (${response.status}) ${await response.text()}`);
    return response.json();
  }
  async function reload() { const data = await api<{ items: Asset[] }>(base); setAssets(data.items); }
  useEffect(() => { void reload().catch(e => setError(String(e))); }, [base, credential]); // eslint-disable-line react-hooks/exhaustive-deps
  async function act(key: string, fn: (requestId: string) => Promise<void>) {
    setBusy(true); setError(""); const id = intents.current[key] ?? crypto.randomUUID(); intents.current[key] = id;
    try { await fn(id); delete intents.current[key]; await reload(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <section className="border rounded p-4 space-y-3"><h2>DAAS 合成资产 · 准备与人工执行</h2>
    <p>规划 RULE/HUMAN；分发 LOCAL_SIMULATED，仅记录本地目标可选，不表示真实执行机传输。模型 NOT_EXECUTED；INCREMENTAL_AGENT_VALUE=NOT_VALIDATED。</p>
    <p>原测试 verdict 与独立 Go 工作流评价分别展示；发现原测试 FAIL 不代表工作流评价 FAIL。工作流评价请在 Go 结果页面核对。</p>
    {plan.tests.filter(t => t.test_kind === "DAAS_CASE").map(t => <Button key={t.test_id} disabled={busy || assets.some(a => a.workflow_source.test_id === t.test_id)} onClick={() => act(`prepare:${t.test_id}`, async request_id => { await api(base, { request_id, plan_digest: plan.plan_digest, test_id: t.test_id, executor_ref: "local-integer-executor@1" }); })}>构造并记录模拟分发 · {t.test_id}</Button>)}
    <Button variant="secondary" disabled={busy} onClick={() => reload().catch(e => setError(String(e)))}>刷新已准备资产</Button>
    {error && <p role="alert">{error}</p>}
    {assets.map(a => <article key={a.instance_id} className="border rounded p-3 space-y-2"><h3>资产 {a.instance_id}</h3><p>{a.execution_status === "NOT_EXECUTED" ? "未执行" : "已执行"} · {a.distribution.mode} · {a.distribution.target_ref}</p>
      <p className="break-all">asset_digest：{a.asset_digest}</p><pre className="whitespace-pre-wrap">{JSON.stringify({ case_asset: a.case_asset, workflow_source: a.workflow_source, distribution: a.distribution }, null, 2)}</pre>
      <Button disabled={busy} onClick={() => act(`execute:${a.instance_id}`, async request_id => { await api(`/api/agentcore/ci/scenarios/${a.instance_id}/executions`, { request_id, ...a.execution_intent, asset_digest: a.asset_digest }); await onExecuted(); })}>执行此资产</Button>
      {a.executions.map(e => <p key={e.execution_id}>原测试 {e.outcome} · CIExecution {e.execution_id} {e.incident_id && <Link className="underline" href={`/ci-incidents/${e.incident_id}`}>查看同一执行的 Incident</Link>}</p>)}
    </article>)}
  </section>;
}
