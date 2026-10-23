"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Textarea } from "@/components/ui/Textarea";

type Doc = { source_ref: string; title: string; content: string; version: string; content_digest: string };
type Finding = { finding_id: string; historical_feature_id: string; coverage_class: string; rationale: string; uncertainties: string[]; supporting_evidence: { source_ref: string }[]; counter_evidence: { source_ref: string }[]; recommended_tests: { test_id: string; preconditions: string; observation: string; expected: string; operations: unknown[] }[] };
type Report = { report_id: string; report_version: number; report_digest: string; candidate_kind: string; status: string; rejection: string | null; raw_output: string; report: { findings: Finding[] } | null; coverage_plan?: Plan | null; executions?: { execution_id: string; test_id: string }[] };
type Task = { task_id: string; title: string; documents: Doc[]; exposed: boolean; reports: { report_id: string; candidate_kind: string; report_version: number }[] };
type Plan = { plan_id: string; plan_digest: string; tests: { test_id: string; finding_id: string }[]; decisions: (Review & { finding_id: string })[] };
type Review = { decision: "ACCEPT" | "REJECT" | "NEEDS_CLARIFICATION"; reason: string };

export default function FeatureImpactPage() {
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);
  const [credential, setCredential] = useState("");
  const [verified, setVerified] = useState(false);
  const [tasks, setTasks] = useState<{ task_id: string; title: string }[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [raw, setRaw] = useState("");
  const [reviews, setReviews] = useState<Record<string, Review>>({});
  const [plan, setPlan] = useState<Plan | null>(null);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function api<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(`/api/agentcore/feature-impact${path}`, { method: body ? "POST" : "GET", cache: "no-store", headers: { Authorization: `Bearer ${credential}`, ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
    if (!response.ok) throw new Error(`请求失败 (${response.status}) ${await response.text()}`);
    return response.json();
  }
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError("");
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }
  async function loadTasks() { const data = await api<{ items: typeof tasks }>("/tasks"); setTasks(data.items); setVerified(true); }
  async function openTask(id: string) { setTask(await api<Task>(`/tasks/${id}`)); setReport(null); setPlan(null); setResult(null); setReviews({}); }
  function showReport(value: Report) {
    setReport(value);
    setReviews(Object.fromEntries((value.coverage_plan?.decisions ?? []).map(({ finding_id, decision, reason }) => [finding_id, { decision, reason }])));
    setPlan(value.coverage_plan ?? null);
    setResult(null);
  }
  function decision(id: string, patch: Partial<Review>) { setReviews((old) => ({ ...old, [id]: { decision: old[id]?.decision ?? "NEEDS_CLARIFICATION", reason: old[id]?.reason ?? "", ...patch } })); }
  return <main className="mx-auto max-w-5xl space-y-5 p-6">
    <h1 className="text-2xl font-bold">历史影响与测试覆盖工作台</h1>
    <p>开发演练资料已曝光，不能作为最终盲态评测。真实 Agent：NOT_EXECUTED；增量价值：NOT_VALIDATED。模型计量约束已后置。</p>
    <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">身份与任务</h2></header><div className="space-y-3">
      <Input aria-label="HUMAN 访问凭据" type="password" autoComplete="off" placeholder="HUMAN Bearer token（仅保存在当前页面内存）" value={credential} disabled={!ready || busy} onChange={(e) => { setCredential(e.target.value); setVerified(false); setTasks([]); setTask(null); setReport(null); setPlan(null); setResult(null); }} />
      <Button disabled={busy || !credential} onClick={() => action(loadTasks)}>验证身份并读取任务</Button>
      {verified && <Button disabled={busy} onClick={() => action(async () => { const t = await api<Task>("/tasks", { request_id: crypto.randomUUID() }); await loadTasks(); await openTask(t.task_id); })}>创建冻结演练任务</Button>}
      <div className="flex flex-wrap gap-2">{tasks.map((t) => <Button variant="secondary" key={t.task_id} disabled={busy} onClick={() => action(() => openTask(t.task_id))}>{t.title} · {t.task_id.slice(0, 8)}</Button>)}</div>
    </div></section>
    {error && <p role="alert" className="text-red-600">{error}</p>}
    {task && <>
      <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">{task.title}</h2></header><div className="space-y-3">
        <p>任务 {task.task_id} · {task.exposed ? "已查看执行结果，禁止再次提交事前分析；请创建新任务。" : "事前规划视图，尚未执行"}</p>
        {task.documents.map((d) => <details key={d.source_ref}><summary className="cursor-pointer">{d.title} · {d.source_ref} · v{d.version}</summary><p className="whitespace-pre-wrap py-2">{d.content}</p><code className="break-all text-xs">{d.content_digest}</code><Button size="sm" variant="secondary" disabled={busy} onClick={() => action(async () => setResult(await api(`/tasks/${task.task_id}/evidence?source_ref=${encodeURIComponent(d.source_ref)}`)))}>读取精确原文</Button></details>)}
      </div></section>
      <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">事前影响分析</h2></header><div className="space-y-3">
        <Button disabled={busy || task.exposed} onClick={() => action(async () => { showReport(await api<Report>(`/tasks/${task.task_id}/rule-reports`, { request_id: crypto.randomUUID() })); setTask(await api<Task>(`/tasks/${task.task_id}`)); })}>执行确定性规则分析</Button>
        <p>Agent 正常入口与调查工具已注册；本轮不启动模型，不产生 Agent 报告。</p>
        <Textarea aria-label="人工影响报告" rows={8} value={raw} onChange={(e) => setRaw(e.target.value)} placeholder="输入 feature-impact-report.v1 JSON。可查看规则报告的原始格式，再独立填写 HUMAN 报告。" />
        <Button disabled={busy || task.exposed || !raw.trim()} onClick={() => action(async () => { showReport(await api<Report>(`/tasks/${task.task_id}/reports`, { request_id: crypto.randomUUID(), raw_output: raw })); setTask(await api<Task>(`/tasks/${task.task_id}`)); })}>提交人工原始报告</Button>
        <div className="flex gap-2">{task.reports.map((r) => <Button variant="secondary" key={r.report_id} disabled={busy} onClick={() => action(async () => showReport(await api<Report>(`/reports/${r.report_id}`)))}>{r.candidate_kind} v{r.report_version}</Button>)}</div>
      </div></section>
    </>}
    {report && <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">{report.candidate_kind} 报告 v{report.report_version} · {report.status}</h2></header><div className="space-y-4">
      <p className="break-all text-xs">报告摘要：{report.report_digest}</p>
      {report.rejection && <p role="alert">拒收原因：{report.rejection}；原始输出已保留。</p>}
      <details><summary>原始报告（不会被审核覆盖）</summary><pre className="max-h-96 overflow-auto whitespace-pre-wrap text-xs">{report.raw_output}</pre></details>
      {report.report?.findings.map((f) => <section key={f.finding_id} className="space-y-2 rounded border p-3">
        <h3 className="font-semibold">{f.historical_feature_id} · {f.coverage_class}</h3><p>{f.rationale}</p>
        <p>支持：{f.supporting_evidence.map((r) => r.source_ref).join("、")}；反证：{f.counter_evidence.map((r) => r.source_ref).join("、") || "无"}</p>
        {f.uncertainties.length > 0 && <p>待澄清：{f.uncertainties.join("；")}</p>}
        {f.recommended_tests.map((t) => <details key={t.test_id}><summary>{t.test_id} · 建议黑盒测试</summary><p>{t.preconditions}</p><pre>{JSON.stringify(t.operations, null, 2)}</pre><p>{t.observation}；{t.expected}</p></details>)}
        <select aria-label={`${f.historical_feature_id} 审核决定`} value={reviews[f.finding_id]?.decision ?? "NEEDS_CLARIFICATION"} disabled={busy || !!plan} onChange={(e) => decision(f.finding_id, { decision: e.target.value as Review["decision"] })} className="rounded border p-2"><option value="NEEDS_CLARIFICATION">NEEDS_CLARIFICATION · 需要澄清</option><option value="ACCEPT">ACCEPT · 采纳</option><option value="REJECT">REJECT · 拒绝</option></select>
        <Input aria-label={`${f.historical_feature_id} 审核理由`} placeholder="填写审核理由" value={reviews[f.finding_id]?.reason ?? ""} disabled={busy || !!plan} onChange={(e) => decision(f.finding_id, { reason: e.target.value })} />
      </section>)}
      {report.status === "SEALED" && <Button disabled={busy || !!plan || !!task?.exposed} onClick={() => action(async () => setPlan(await api<Plan>(`/reports/${report.report_id}/reviews`, { request_id: crypto.randomUUID(), report_version: report.report_version, report_digest: report.report_digest, decisions: report.report!.findings.map((f) => ({ finding_id: f.finding_id, ...reviews[f.finding_id] })) })))}>提交审核并封存覆盖计划</Button>}
    </div></section>}
    {plan && <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">已封存覆盖计划 · 事后验证</h2></header><div className="space-y-3"><p>审核不会自动执行；以下按钮单独触发已获准的操作序列。</p><p>{plan.plan_id}</p>{plan.tests.map((t) => <Button disabled={busy} key={t.test_id} onClick={() => action(async () => { setResult(await api(`/coverage-plans/${plan.plan_id}/executions`, { request_id: crypto.randomUUID(), plan_digest: plan.plan_digest, test_id: t.test_id })); if (task) setTask(await api<Task>(`/tasks/${task.task_id}`)); })}>执行 {t.test_id}</Button>)}{!plan.tests.length && <p>计划没有获准测试；待澄清项不会自动执行。</p>}</div></section>}
    {!!report?.executions?.length && <section><h2>持久执行记录</h2>{report.executions.map((e) => <Button variant="secondary" disabled={busy} key={e.execution_id} onClick={() => action(async () => setResult(await api(`/executions/${e.execution_id}`)))}>查看 {e.test_id} · {e.execution_id.slice(0, 8)}</Button>)}</section>}
    {result !== null && <section className="space-y-3 rounded border border-border p-4"><header><h2 className="text-lg font-semibold">精确资料 / 业务验证结果</h2></header><div><pre className="overflow-auto whitespace-pre-wrap text-sm">{JSON.stringify(result, null, 2)}</pre></div></section>}
  </main>;
}
