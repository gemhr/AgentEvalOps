"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { ciPath, login, logout, request, useCIIdentity, type Incident, type Scenario } from "@/lib/api/ci-incidents";

export function CIIdentity() {
  const identity = useCIIdentity(); const cache = useQueryClient();
  const [credential, setCredential] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function verify() {
    setBusy(true); setError(""); cache.removeQueries({ queryKey: ["ci"] });
    try { await login(credential); setCredential(""); } catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  return <section className="rounded border border-border p-4 space-y-3">
    <p>使用现有 HUMAN 测试用户 JWT。凭据仅保存在内存，刷新后请恢复同一用户身份。</p>
    {identity ? <div><span>用户 {identity.user_id} · 租户 {identity.tenant_id}</span> <Button onClick={() => { logout(); cache.removeQueries({ queryKey: ["ci"] }); }}>退出</Button></div>
      : <div className="flex gap-3"><input aria-label="HUMAN JWT" type="password" autoComplete="off" className="border rounded p-2 flex-1" value={credential} onChange={(event) => setCredential(event.target.value)} /><Button disabled={busy || !credential} onClick={verify}>验证身份</Button></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

export default function IncidentWorkbench() {
  const identity = useCIIdentity(); const router = useRouter(); const cache = useQueryClient();
  const scope = ["ci", identity?.tenant_id, identity?.user_id];
  const scenarios = useQuery({ queryKey: [...scope, "scenarios"], queryFn: () => request<{ items: Scenario[] }>(ciPath("/scenarios")), enabled: !!identity });
  const incidents = useQuery({ queryKey: [...scope, "incidents"], queryFn: () => request<{ items: Incident[] }>(ciPath("/incidents")), enabled: !!identity });
  const [key, setKey] = useState("report_pipeline_failure"); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [intent, setIntent] = useState<{ scenario_key: string; request_id: string; executionRequest: string } | null>(null);
  useEffect(() => { setIntent(null); setError(""); }, [identity]);
  async function execute() {
    const current = intent ?? { scenario_key: key, request_id: crypto.randomUUID(), executionRequest: crypto.randomUUID() };
    setIntent(current); setBusy(true); setError("");
    try {
      const instance = await request<{ instance_id: string }>(ciPath("/scenarios"), { scenario_key: current.scenario_key, request_id: current.request_id });
      const execution = await request<{ incident_id: string | null; outcome: string }>(ciPath(`/scenarios/${instance.instance_id}/executions`), { request_id: current.executionRequest });
      setIntent(null); await cache.invalidateQueries({ queryKey: scope });
      if (execution.incident_id) router.push(`/ci-incidents/${execution.incident_id}`);
    } catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  return <main className="max-w-5xl mx-auto p-8 space-y-6"><h1 className="text-2xl">CI 故障调查工作台</h1><nav className="flex gap-4"><Link href="/ci-incidents/nightly">夜间 CI</Link><Link href="/ci-incidents/notifications">我的通知</Link></nav><p>可直接选择场景，人工预览修复、确认、回归与关闭；无需 Agent 报告。</p><CIIdentity />
    {identity && <><section className="space-y-3"><h2>场景执行</h2><select aria-label="演练场景" className="border rounded p-2" value={key} disabled={busy || !!intent} onChange={(event) => setKey(event.target.value)}>
      {scenarios.data?.items.map((item) => <option key={item.scenario_key} value={item.scenario_key}>{item.title}</option>)}</select> <Button disabled={busy} onClick={execute}>{busy ? "执行中…" : intent ? "重试本次执行" : "执行场景"}</Button>
      {error && <p role="alert">{error}</p>}{scenarios.error && <p role="alert">{String(scenarios.error)}</p>}</section>
    <section className="space-y-3"><h2>Incident 历史</h2>{incidents.error && <p role="alert">{String(incidents.error)}</p>}
      {incidents.data?.items.map((item) => <article key={item.incident_id} className="border rounded p-4"><Link className="underline" href={`/ci-incidents/${item.incident_id}`}>{item.title}</Link><p>{item.status} · {item.created_at}</p></article>)}
      {incidents.data?.items.length === 0 && <p>暂无事件</p>}</section></>}
  </main>;
}
