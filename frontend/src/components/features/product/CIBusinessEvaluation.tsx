"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createCIBusinessRun, readCIBusinessResult, productCommand, productGet, productPath, type ProductRecord } from "@/lib/api/product";
import { useProductPrincipal } from "./ProductShell";

export function CIBusinessEvaluation({ project }: { project: string }) {
  const principal = useProductPrincipal();
  const [suite, setSuite] = useState(""); const [version, setVersion] = useState("v1"); const [rule, setRule] = useState("parser-compatibility.v1");
  const [run, setRun] = useState(""); const [result, setResult] = useState(""); const [policy, setPolicy] = useState(""); const [baseline, setBaseline] = useState(""); const [candidate, setCandidate] = useState(""); const [gate, setGate] = useState("");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [intent, setIntent] = useState<{ command: string; suite: { entity_id: string; version: string }; rule: string } | null>(null);
  const [gateIntent, setGateIntent] = useState<{ command: string; baseline: string; candidate: string; policy: string } | null>(null);
  useEffect(() => { const q = new URLSearchParams(window.location.search); setSuite(q.get("ci_suite") ?? ""); setRun(q.get("ci_run") ?? ""); setResult(q.get("ci_result") ?? ""); setPolicy(q.get("ci_policy") ?? ""); setBaseline(q.get("ci_baseline") ?? ""); setCandidate(q.get("ci_candidate") ?? ""); setGate(q.get("ci_gate") ?? ""); }, [project]);
  function remember(values: Record<string, string>) { const url = new URL(window.location.href); for (const [k, v] of Object.entries(values)) url.searchParams.set("ci_" + k, v); window.history.replaceState(null, "", url); }
  const key = ["ci-business", principal?.principal_id, project];
  const suiteView = useQuery({ queryKey: [...key, "suite", suite, version], queryFn: () => productGet<ProductRecord>(productPath(project, `suites/${encodeURIComponent(suite)}/versions/${encodeURIComponent(version)}`)), enabled: !!principal && !!suite, retry: false });
  const state = useQuery({ queryKey: [...key, "run", run], queryFn: () => productGet<{ pipeline_state: string; results: { id: string; case_id: string; evaluation_verdict: string; score: number | null }[] }>(productPath(project, `runs/${run}`)), enabled: !!principal && !!run, retry: false });
  const detail = useQuery({ queryKey: [...key, "result", result], queryFn: () => readCIBusinessResult(project, result), enabled: !!principal && !!result, retry: false });
  const gateView = useQuery({ queryKey: [...key, "gate", gate], queryFn: () => productGet<ProductRecord>(productPath(project, `gates/${gate}`)), enabled: !!principal && !!gate, retry: false });
  const comparison = useQuery({ queryKey: [...key, "comparison", gate], queryFn: () => productGet<ProductRecord>(productPath(project, `gates/${gate}/cases?limit=100`)), enabled: !!principal && !!gate, retry: false });
  async function create() {
    const current = intent ?? { command: crypto.randomUUID(), suite: { entity_id: suite, version }, rule }; setIntent(current); setBusy(true); setError("");
    try { const created = await createCIBusinessRun(project, current.suite, current.rule, current.command); setRun(created.id); setIntent(null); if (current.rule.endsWith(".v1")) { setBaseline(created.id); remember({ baseline: created.id, run: created.id, suite }); } else { setCandidate(created.id); remember({ candidate: created.id, run: created.id, suite }); } }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function compare() {
    const current = gateIntent ?? { command: crypto.randomUUID(), baseline, candidate, policy }; setGateIntent(current); setBusy(true); setError("");
    try { const created = await productCommand<ProductRecord>(productPath(project, "gates"), { baseline: { kind: "RUN_SET", runs: [current.baseline] }, candidate: { kind: "RUN_SET", runs: [current.candidate] }, policy: { entity_id: current.policy, version: "v1" } }, current.command); const id = String(created.gate_id ?? created.id); setGate(id); setGateIntent(null); remember({ gate: id, baseline, candidate, policy }); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  function decodedEvidence() { return detail.data?.input.records.map(f => { try { return { sha256: f.sha256, source: JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(f.canonical_bytes_b64), c => c.charCodeAt(0)))) }; } catch { return { error: "来源正文无法解码", sha256: f.sha256 }; } }); }
  const errors = error || String(suiteView.error ?? state.error ?? detail.error ?? gateView.error ?? comparison.error ?? "");
  return <section className="border p-4 space-y-4"><h2 className="text-xl">模拟 CI 业务规则质量评测</h2><p>冻结的授权模拟证据；不是 Agent 诊断准确率。真实执行确定性规则，参考仅供 Evaluator 判分；未知和不适用保留 null。v2 仅离线，不改变夜间在线分组。</p>
    <fieldset disabled={busy || !!intent} className="flex flex-wrap gap-3"><label>业务 Suite ID <input aria-label="业务 Suite ID" className="border p-2" value={suite} onChange={e => { setSuite(e.target.value); remember({ suite: e.target.value }); }} /></label><label>Suite 版本 <input aria-label="业务 Suite 版本" className="border p-2" value={version} onChange={e => setVersion(e.target.value)} /></label><label>实际规则 <select aria-label="实际业务规则" value={rule} onChange={e => setRule(e.target.value)}><option>parser-compatibility.v1</option><option>parser-compatibility.v2</option></select></label></fieldset>
    <button className="border p-2" disabled={busy || !suite} onClick={create}>{intent ? "重试同一业务 Run" : "创建真实业务 Run"}</button>{intent && !busy && <button onClick={() => setIntent(null)}>取消待重试创建</button>}
    {suiteView.data && <details><summary>冻结 Suite / Dataset 版本</summary><pre className="whitespace-pre-wrap">{JSON.stringify(suiteView.data, null, 2)}</pre></details>}
    <label className="block">已保存业务 Run ID <input aria-label="已保存业务 Run ID" className="border p-2" value={run} onChange={e => { setRun(e.target.value); remember({ run: e.target.value }); }} /></label><button className="border p-2" disabled={!run} onClick={() => state.refetch()}>读取已保存业务结果</button>
    {state.data && <><p>Run {run} · {state.data.pipeline_state}</p>{state.data.results.map(r => <p key={r.id}><button className="underline" onClick={() => { setResult(r.id); remember({ result: r.id }); }}>{r.id} · {r.evaluation_verdict} · {r.score ?? "null"}</button> · case {r.case_id}</p>)}</>}
    {detail.data && <section><h3>Reference / Actual 与冻结证据</h3><p>{detail.data.verdict} · score {detail.data.score ?? "null"} · {detail.data.reason}</p><pre className="whitespace-pre-wrap">{JSON.stringify({ case: detail.data.case, reference: detail.data.reference, actual: detail.data.actual, provenance: detail.data.provenance }, null, 2)}</pre><details><summary>原始 canonical 来源字节解码与摘要</summary><pre className="whitespace-pre-wrap">{JSON.stringify(decodedEvidence(), null, 2)}</pre></details></section>}
    <h3>使用现有 Gate 比较两个版本</h3><fieldset disabled={busy || !!gateIntent} className="space-y-2">{[["基线业务 Run", baseline, setBaseline], ["候选业务 Run", candidate, setCandidate], ["业务 Policy ID", policy, setPolicy]].map(([label, value, setter]) => <label className="block" key={String(label)}>{String(label)} <input aria-label={String(label)} className="border p-2" value={String(value)} onChange={e => (setter as (v: string) => void)(e.target.value)} /></label>)}</fieldset>
    <button className="border p-2" disabled={busy || !baseline || !candidate || !policy} onClick={compare}>{gateIntent ? "重试相同 Gate" : "创建业务版本 Gate"}</button>
    {gateView.data && <pre className="whitespace-pre-wrap">{JSON.stringify(gateView.data, null, 2)}</pre>}{comparison.data && <details open><summary>已保存案例对比：改善、退化与未判定</summary><pre className="whitespace-pre-wrap">{JSON.stringify(comparison.data, null, 2)}</pre></details>}
    {errors && <p role="alert">{errors}</p>}
  </section>;
}
