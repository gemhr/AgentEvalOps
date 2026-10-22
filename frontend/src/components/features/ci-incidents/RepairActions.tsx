"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ciPath, request, type CurrentState, type Detail, type ManualAction } from "@/lib/api/ci-incidents";

export default function RepairActions({ incidentId, detail, state, onChanged }: {
  incidentId: string; detail: Detail; state: CurrentState; onChanged: () => Promise<void>;
}) {
  const [kind, setKind] = useState(""); const [draft, setDraft] = useState<ManualAction | null>(null);
  const [intent, setIntent] = useState<object | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const option = state.allowed_actions.find((item) => item.action_kind === kind) ?? state.allowed_actions[0];
  const base = ciPath(`/incidents/${incidentId}/actions`);
  const stale = !!draft && draft.status === "PREVIEWED" && draft.base_state_revision !== state.state_revision;
  async function preview() {
    if (!option) return;
    const body = intent ?? { request_id: crypto.randomUUID(), action_kind: option.action_kind,
      target_case_id: state.cases[0].case_id, parameters: option.parameters };
    setIntent(body); setBusy(true); setError("");
    try { setDraft(await request<ManualAction>(base, body)); setIntent(null); await onChanged(); }
    catch (error) { setError(String(error)); } finally { setBusy(false); }
  }
  async function confirm() {
    if (!draft) return;
    setBusy(true); setError("");
    try { setDraft(await request<ManualAction>(`${base}/${draft.action_id}/confirm`, { confirmed: true, preview_digest: draft.preview_digest })); await onChanged(); }
    catch (error) { setError(String(error)); await onChanged(); } finally { setBusy(false); }
  }
  return <section className="border rounded p-4 space-y-4"><h2 className="text-lg">人工修复 · HUMAN_MANUAL</h2>
    <p>操作由当前人类用户选择并确认，不是 Agent 建议或 Tool 审批。研发交付仅修改模拟状态，不修改真实代码。</p>
    {detail.status !== "CLOSED" && <div className="flex flex-wrap gap-3"><select aria-label="人工修复动作" className="border rounded p-2" disabled={busy} value={option?.action_kind ?? ""} onChange={(event) => { setKind(event.target.value); setIntent(null); setDraft(null); }}>
      {state.allowed_actions.map((item) => <option key={item.action_kind} value={item.action_kind}>{item.title}</option>)}</select>
      <Button onClick={preview} disabled={busy || !option}>{intent ? "重试本次预览" : "预览修复"}</Button></div>}
    {option && detail.status !== "CLOSED" && <p>有限参数：{JSON.stringify(option.parameters)}</p>}
    {error && <p role="alert">{error}。冲突后请重新预览，再明确确认。</p>}
    {draft && <div className="space-y-3"><h3>{draft.status} · {draft.action_id}</h3><p>预览基于版本 {draft.base_state_revision}，当前版本 {state.state_revision}</p>
      <p>影响环境：{draft.preview_payload.environment_id}；用例：{draft.preview_payload.case_ids.join("、")}</p>
      <table className="w-full text-left"><thead><tr><th>字段</th><th>修改前</th><th>修改后</th></tr></thead><tbody>
        {draft.preview_payload.changes.map((change) => <tr key={change.path.join(".")}><td>{change.path.join(".")}</td><td>{JSON.stringify(change.before)}</td><td>{JSON.stringify(change.after)}</td></tr>)}
      </tbody></table>
      {stale && <p role="alert">预览已过时，请重新预览。</p>}
      {draft.status === "PREVIEWED" && <Button onClick={confirm} disabled={busy || stale || detail.status === "CLOSED"}>确认执行修复</Button>}
      {draft.status === "APPLIED" && <p>已应用到版本 {draft.applied_state_revision}，仍需单例和组合验证。</p>}
    </div>}
    <h3>人工操作历史</h3>{detail.actions.length === 0 && <p>尚无操作</p>}
    {detail.actions.map((action) => <article key={action.action_id} className="border rounded p-3 space-y-1"><p>{action.action_kind} · {action.source_kind} · {action.status}</p>
      <p className="break-all">{action.action_id} · 创建者 {action.created_by_user_id} · {action.created_at}</p>
      <p>版本 {action.base_state_revision} → {action.applied_state_revision ?? "未应用"}{action.stale ? " · 预览过时" : ""}</p>
      {action.confirmed_at && <p>确认者 {action.confirmed_by_user_id} · {action.confirmed_at}</p>}
      <pre className="whitespace-pre-wrap break-all">{JSON.stringify(action.preview_payload.changes, null, 2)}</pre>
    </article>)}
  </section>;
}
