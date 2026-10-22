"use client";

import { useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { ciPath, request, type IncidentGroup, type DecisionIntent } from "@/lib/api/ci-nightly";

export default function IncidentGroups({ batchId, groups, refresh }: { batchId: string; groups: IncidentGroup[]; refresh: () => Promise<unknown> }) {
  return <section className="space-y-4"><h2>证据候选关联与独立样本</h2><p>确定性证据匹配，只确认关联，不是模型自主归因或最终根因判断。负责人视图仅本人范围，不显示其他成员详情。</p>
    {groups.map(group => <GroupCard key={group.group_id} batchId={batchId} group={group} refresh={refresh} />)}</section>;
}

function GroupCard({ batchId, group, refresh }: { batchId: string; group: IncidentGroup; refresh: () => Promise<unknown> }) {
  const [selected, setSelected] = useState<string[]>([]); const [reason, setReason] = useState("");
  const [intent, setIntent] = useState<DecisionIntent | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function decide(kind: DecisionIntent["kind"]) {
    const body = intent ?? { request_id: crypto.randomUUID(), expected_group_revision: group.revision, kind, incident_ids: selected, reason };
    setIntent(body); setBusy(true); setError("");
    try { await request(ciPath(`/nightly/batches/${batchId}/groups/${group.group_id}/decisions`), body); setIntent(null); setSelected([]); setReason(""); await refresh(); }
    catch (e) { setError(String(e)); await refresh(); } finally { setBusy(false); }
  }
  return <article className="border rounded p-4 space-y-3"><h3>{group.state} · {group.kind} · 版本 {group.revision}</h3><p>{group.group_id} · {group.source}</p><details><summary>采集字段签名</summary><pre className="whitespace-pre-wrap">{JSON.stringify(group.signature, null, 2)}</pre></details>
    <fieldset disabled={busy || !!intent}>{group.members.map(m => <div key={m.incident_id}><label><input aria-label={`选择成员 ${m.incident_id}`} type="checkbox" disabled={m.decision === "EXCLUDED"} checked={selected.includes(m.incident_id)} onChange={e => setSelected(e.target.checked ? [...selected, m.incident_id] : selected.filter(i => i !== m.incident_id))} /> {m.environment_id} · {m.software_version} · {m.decision} · {m.status}</label><p>{m.incident_id} · 负责人 {m.owner_user_id} {m.can_open && <Link className="underline" href={`/ci-incidents/${m.incident_id}`}>查看本人故障</Link>}</p></div>)}
      <label>关联决定理由 <input aria-label={`关联决定理由 ${group.group_id}`} className="border p-2 w-full" maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label></fieldset>
    <div className="flex gap-3">{(["CONFIRM", "EXCLUDE", "SPLIT"] as const).map(kind => <Button key={kind} disabled={busy || (!!intent && intent.kind !== kind) || (!intent && (!selected.length || !reason.trim()))} onClick={() => decide(kind)}>{intent?.kind === kind ? `重试 ${kind}` : kind}</Button>)}{intent && !busy && <Button onClick={() => setIntent(null)}>取消待重试决定</Button>}</div>
    {error && <p role="alert">{error}</p>}<details><summary>权限范围内的人工历史</summary><pre className="whitespace-pre-wrap">{JSON.stringify(group.history, null, 2)}</pre></details>
  </article>;
}
