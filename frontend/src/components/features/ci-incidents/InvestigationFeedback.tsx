"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ciPath, request, useCIIdentity, type Investigation } from "@/lib/api/ci-incidents";
import { Button } from "@/components/ui/Button";
type Feedback = { feedback_id: string; investigation_id: string; supersedes: string | null; created_at: string; request: { decision: string; reason: string }; binding: { report_version: number; report_digest: string; source: string; review: string; verification: string } };
export default function InvestigationFeedback({ incidentId, investigation }: { incidentId: string; investigation?: Investigation }) {
  const identity = useCIIdentity();
  const [decision, setDecision] = useState("ACCEPT"), [reason, setReason] = useState(""), [target, setTarget] = useState("");
  const [message, setMessage] = useState(""), [busy, setBusy] = useState(false), [pending, setPending] = useState<object | null>(null);
  const path = ciPath(`/incidents/${encodeURIComponent(incidentId)}/feedback`);
  const history = useQuery({ queryKey: ["ci-feedback", identity?.tenant_id, identity?.user_id, incidentId], queryFn: () => request<{ items: Feedback[] }>(path), enabled: !!identity });
  const items = history.data?.items ?? [], superseded = new Set(items.map(item => item.supersedes));
  async function submit() {
    if (!investigation) return;
    const body = pending ?? { request_id: crypto.randomUUID(), investigation_id: investigation.investigation_id, report_version: investigation.report_version, decision, reason, supersedes: target || null };
    setPending(body); setBusy(true); setMessage("");
    try { await request<Feedback>(path, body); setPending(null); setTarget(""); setMessage("反馈已记录"); await history.refetch(); }
    catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  return <section className="border rounded p-4 space-y-3"><h2>调查报告人工反馈</h2><p>受控模拟来源；人工评价不代表根因已验证。</p>
    {investigation?.report && <><p>反馈对象：v{investigation.report_version} · {investigation.investigation_id}</p>
      <fieldset disabled={busy || !!pending}>
        <label>评价 <select value={decision} onChange={e => setDecision(e.target.value)}><option value="ACCEPT">认可建议</option><option value="REJECT">拒绝建议</option><option value="NEEDS_CLARIFICATION">需要进一步调查</option><option value="WITHDRAW">撤回此前反馈</option></select></label>
        <label>修订对象 <select value={target} onChange={e => setTarget(e.target.value)}><option value="">新增独立反馈</option>{items.filter(item => item.investigation_id === investigation.investigation_id && !superseded.has(item.feedback_id)).map(item => <option key={item.feedback_id} value={item.feedback_id}>{item.request.decision} · {item.created_at}</option>)}</select></label>
        <label className="block">纠正理由（可选）<textarea className="block border w-full" value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} /></label>
      </fieldset><Button disabled={busy || (!pending && decision === "WITHDRAW" && !target)} onClick={submit}>{pending ? "重试同一反馈" : "提交反馈"}</Button>
      {pending && !busy && <Button onClick={() => setPending(null)}>取消重试并编辑</Button>}</>}
    {message && <p role="status">{message}</p>}{history.error && <p role="alert">{String(history.error)}</p>}
    <h3>反馈历史</h3>{items.map(item => <div key={item.feedback_id}><p>v{item.binding.report_version} · {item.request.decision} · {superseded.has(item.feedback_id) ? "已被修订" : "当前记录"} · {item.created_at}</p><p>{item.request.reason}</p><p className="break-all">报告摘要：{item.binding.report_digest}</p><p>{item.binding.source} / {item.binding.review} / {item.binding.verification}</p></div>)}
  </section>;
}
