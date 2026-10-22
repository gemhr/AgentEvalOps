"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ciPath, request, type CloseIntent, type CurrentState, type Detail } from "@/lib/api/ci-incidents";

type VerificationIntent = { request_id: string; mode: string; case_ids: string[]; expected_state_revision: number };

export default function VerificationPanel({ incidentId, detail, state, onChanged, onRead }: {
  incidentId: string; detail: Detail; state: CurrentState; onChanged: () => Promise<void>; onRead: (executionId: string, evidenceId: string) => void;
}) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [intents, setIntents] = useState<Record<string, VerificationIntent>>({});
  const [rootCause, setRootCause] = useState(""); const [fix, setFix] = useState(""); const [checked, setChecked] = useState(false);
  const [closeIntent, setCloseIntent] = useState<CloseIntent | null>(null);
  const base = ciPath(`/incidents/${incidentId}`);
  async function verify(mode: "SINGLE" | "GROUP") {
    const body = intents[mode] ?? { request_id: crypto.randomUUID(), mode, expected_state_revision: state.state_revision,
      case_ids: mode === "SINGLE" ? [state.required_case_ids[0]] : state.required_case_ids };
    setIntents((value) => ({ ...value, [mode]: body })); setBusy(true); setError("");
    try { await request(`${base}/verifications`, body); setIntents((value) => { const next = { ...value }; delete next[mode]; return next; }); await onChanged(); }
    catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  async function close() {
    const gate = detail.closure_eligibility;
    if (!gate.action_id || !gate.single_verification_id || !gate.group_verification_id) return;
    const body = closeIntent ?? { request_id: crypto.randomUUID(), expected_state_revision: state.state_revision,
      action_id: gate.action_id, single_verification_id: gate.single_verification_id, group_verification_id: gate.group_verification_id,
      root_cause: rootCause.trim(), fix_confirmation: fix.trim(), regression_confirmed: checked };
    setCloseIntent(body); setBusy(true); setError("");
    try { await request(`${base}/close`, body); setCloseIntent(null); await onChanged(); }
    catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  return <section className="border rounded p-4 space-y-4"><h2 className="text-lg">人工回归验证</h2>
    <p>原始 FAIL 保持不变，每次验证追加新执行。GROUP 使用同版本的两个真实用例结果；验证 FAIL 归属当前事件，不创建重复事件。</p>
    {detail.status !== "CLOSED" && <div className="flex flex-wrap gap-3"><Button disabled={busy} onClick={() => verify("SINGLE")}>{intents.SINGLE ? "重试 SINGLE" : "单独重跑 SINGLE"}</Button>
      <Button disabled={busy} onClick={() => verify("GROUP")}>{intents.GROUP ? "重试 GROUP" : "组合回归 GROUP"}</Button>
      {Object.keys(intents).length > 0 && <Button disabled={busy} onClick={() => setIntents({})}>放弃重试，使用当前版本新建验证意图</Button>}</div>}
    {error && <p role="alert">{error}</p>}
    {detail.verifications.map((batch) => <article key={batch.verification_id} className="border rounded p-3 space-y-2"><h3>{batch.mode} · {batch.outcome} · 版本 {batch.state_revision}</h3>
      <p className="break-all">批次 {batch.verification_id} · 操作人 {batch.created_by_user_id} · {batch.completed_at}</p>
      <p>原执行 {batch.comparison.original_outcome} → 新主用例 {batch.comparison.new_outcome}</p>
      <pre className="whitespace-pre-wrap break-all">{JSON.stringify(batch.comparison.changes, null, 2)}</pre>
      {batch.items.map((item) => <div key={item.execution_id} className="border-t pt-2 space-y-2"><p>{item.case_id} · {item.outcome} · 版本 {item.state_revision}</p><p className="break-all">{item.execution_id}</p>
        <div className="flex flex-wrap gap-2">{item.evidence.map((evidence) => <Button key={evidence.evidence_id} onClick={() => onRead(item.execution_id, evidence.evidence_id)}>{item.case_id} · {evidence.kind}</Button>)}</div>
      </div>)}
    </article>)}
    <h2 className="text-lg">人工关闭确认</h2>
    {detail.status === "CLOSED" && detail.closure ? <div className="space-y-2"><p>已关闭 · {detail.closed_at} · 人类 {detail.closed_by_user_id} · 版本 {detail.closure.state_revision}</p>
      <p>来源：{detail.closure.source_kind}</p><p>人工根因：{detail.closure.confirmation.root_cause}</p><p>修复确认：{detail.closure.confirmation.fix_confirmation}</p>
      <pre className="whitespace-pre-wrap break-all">{JSON.stringify(detail.closure, null, 2)}</pre></div>
      : <div className="space-y-3"><p>关闭资格：{detail.closure_eligibility.eligible ? "当前修复、SINGLE 和 GROUP 均满足" : `尚未满足：${detail.closure_eligibility.reasons.join("、")}`}</p>
        <label className="block">人工根因<textarea aria-label="人工根因" className="border rounded p-2 w-full" maxLength={1000} value={rootCause} onChange={(event) => { setRootCause(event.target.value); setCloseIntent(null); }} /></label>
        <label className="block">修复确认<textarea aria-label="修复确认" className="border rounded p-2 w-full" maxLength={1000} value={fix} onChange={(event) => { setFix(event.target.value); setCloseIntent(null); }} /></label>
        <label className="block"><input type="checkbox" aria-label="确认组合回归已检查" checked={checked} onChange={(event) => { setChecked(event.target.checked); setCloseIntent(null); }} /> 我已检查同版本单例及组合回归并明确确认关闭</label>
        <Button disabled={busy || !detail.closure_eligibility.eligible || !rootCause.trim() || !fix.trim() || !checked} onClick={close}>人工关闭事件</Button>
      </div>}
  </section>;
}
