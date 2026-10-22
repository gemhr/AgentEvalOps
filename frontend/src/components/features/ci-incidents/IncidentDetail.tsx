"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { CIIdentity } from "./IncidentWorkbench";
import { ciPath, request, subscribe, terminal, useCIIdentity, type Detail, type Evidence, type Investigation, type Start, type CurrentState } from "@/lib/api/ci-incidents";
import RepairActions from "./RepairActions";
import VerificationPanel from "./VerificationPanel";

export default function IncidentDetail({ incidentId }: { incidentId: string }) {
  const identity = useCIIdentity(); const cache = useQueryClient(); const [progress, setProgress] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [intent, setIntent] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const scope = ["ci", identity?.tenant_id, identity?.user_id, incidentId];
  const base = `/incidents/${encodeURIComponent(incidentId)}`;
  const detail = useQuery({ queryKey: [...scope, "detail"], queryFn: () => request<Detail>(ciPath(base)), enabled: !!identity });
  const currentState = useQuery({ queryKey: [...scope, "state"], queryFn: () => request<CurrentState>(ciPath(`${base}/state`)), enabled: !!identity });
  const latest = detail.data?.investigations[0];
  const investigation = useQuery({ queryKey: [...scope, latest?.investigation_id], enabled: !!identity && !!latest,
    queryFn: () => request<Investigation>(ciPath(`${base}/investigations/${latest!.investigation_id}`)),
    refetchInterval: (query) => terminal(query.state.data) || query.state.error ? false : 2000 });
  const active = !!latest && !terminal(investigation.data) && !investigation.error;
  useEffect(() => {
    if (!identity || !latest || !active) return;
    return subscribe(latest.run_id, setProgress, () => { void investigation.refetch(); });
  // 仅在 Run 或身份改变时重新订阅；订阅不会创建调查。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, latest?.run_id, active]);
  useEffect(() => { setEvidence(null); setError(""); setIntent(null); setProgress(""); }, [identity]);
  async function start() {
    const requestId = intent ?? crypto.randomUUID(); setIntent(requestId); setBusy(true); setError("");
    try { await request<Start>(ciPath(`${base}/investigations`), { request_id: requestId }); setIntent(null); await cache.invalidateQueries({ queryKey: scope }); }
    catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  async function read(id: string) {
    try { setEvidence(await request<Evidence>(ciPath(`${base}/evidence/${encodeURIComponent(id)}`))); }
    catch (error) { setError(String(error)); }
  }
  async function readExecution(executionId: string, evidenceId: string) {
    try { setEvidence(await request<Evidence>(ciPath(`${base}/executions/${executionId}/evidence/${evidenceId}`))); }
    catch (error) { setError(String(error)); }
  }
  async function refreshBusiness() { await Promise.all([detail.refetch(), currentState.refetch()]); }
  const report = investigation.data?.report;
  return <main className="max-w-5xl mx-auto p-8 space-y-6"><Link className="underline" href="/ci-incidents">← 返回工作台</Link><h1 className="text-2xl">Incident 详情</h1><CIIdentity />
    {identity && <>{detail.error && <p role="alert">{String(detail.error)}</p>}{detail.data && <>
      <section className="border rounded p-4 space-y-2"><h2>{detail.data.title}</h2><p>{incidentId} · {detail.data.status} · 原始 CI {detail.data.execution.outcome}</p><p>原始执行 {detail.data.execution.execution_id}</p>
      <Button disabled={busy || active} onClick={start}>{busy ? "启动中…" : intent ? "重试本次调查" : "启动新调查"}</Button></section>
      {error && <p role="alert">{error}</p>}{investigation.error && <p role="alert">{String(investigation.error)}</p>}
      {latest && <section className="space-y-2"><h2>调查 v{latest.report_version}</h2><p>Run {latest.run_id}</p><p>{investigation.data?.investigation_status ?? "读取中"} · {progress}</p>
        {investigation.data?.safe_error_code && <p role="alert">{investigation.data.safe_error_code}。可显式启动新调查。</p>}</section>}
      {report && <section className="border rounded p-4 space-y-3"><h2>{report.diagnosis_status === "INSUFFICIENT_EVIDENCE" ? "证据不足 · 待求证" : "证据支持的根因假设"}</h2>
        <p>失败阶段：{report.failure_stage}</p><p>{report.root_cause_hypothesis}</p><p>推荐责任角色：{report.recommended_owner_role}</p>
        <h3>不确定性</h3><ul>{report.uncertainties.map((item, n) => <li key={n}>{item}</li>)}</ul><h3>建议检查项</h3><ul>{report.recommended_checks.map((item, n) => <li key={n}>{item}</li>)}</ul>
        <h3>报告引用</h3>{report.supporting_evidence.map((ref) => <Button key={ref.evidence_id} onClick={() => read(ref.evidence_id)}>{detail.data?.evidence.find((item) => item.evidence_id === ref.evidence_id)?.kind ?? ref.evidence_id}</Button>)}
      </section>}
      {!report && <p>尚无可用 Agent 诊断，可先人工处置。模型计数接入已后置，人工操作不调用模型。</p>}
      {currentState.error && <p role="alert">{String(currentState.error)}</p>}
      {currentState.data && <><section className="border rounded p-4 space-y-2"><h2 className="text-lg">当前模拟状态 · 版本 {currentState.data.state_revision}</h2>
        <p>读取时间 {currentState.data.observed_at}；这是当前状态，不是原失败时的历史证据。</p><pre className="whitespace-pre-wrap break-all">{JSON.stringify({ environment: currentState.data.environment, cases: currentState.data.cases, report_field_revision: currentState.data.report_field_revision }, null, 2)}</pre></section>
        <RepairActions key={`repair-${identity.user_id}-${incidentId}`} incidentId={incidentId} detail={detail.data} state={currentState.data} onChanged={refreshBusiness} />
        <VerificationPanel key={`verify-${identity.user_id}-${incidentId}`} incidentId={incidentId} detail={detail.data} state={currentState.data} onChanged={refreshBusiness} onRead={readExecution} />
      </>}
      <section className="space-y-3"><h2>原始执行证据</h2>{detail.data.evidence.map((item) => <Button key={item.evidence_id} onClick={() => read(item.evidence_id)}>{item.kind} · {item.availability}</Button>)}</section>
      {evidence && <section className="border rounded p-4"><h2>{evidence.kind} · {evidence.availability}</h2><p className="break-all">digest: {evidence.digest ?? "未采集"}</p><pre className="whitespace-pre-wrap break-all">{JSON.stringify(evidence.content ?? evidence.missing_reason, null, 2)}</pre></section>}
      <section><h2>报告版本历史</h2>{detail.data.investigations.map((item) => <p key={item.investigation_id}>v{item.report_version} · {item.projection_status} · {item.run_id}</p>)}</section>
    </>}</>}
  </main>;
}
