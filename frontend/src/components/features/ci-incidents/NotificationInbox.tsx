"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { useCIIdentity } from "@/lib/api/ci-incidents";
import { CIIdentity } from "./IncidentWorkbench";
import { NightlyNavigation } from "./NightlyWorkbench";
import { ciPath, request, type Inbox } from "@/lib/api/ci-nightly";

export default function NotificationInbox() {
  const identity = useCIIdentity(); const [unreadOnly, setUnreadOnly] = useState(false); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const inbox = useQuery({ queryKey: ["ci", identity?.tenant_id, identity?.user_id, "notifications", unreadOnly], queryFn: () => request<Inbox>(ciPath(`/notifications?unread_only=${unreadOnly}`)), enabled: !!identity });
  async function read(id: string) { setBusy(true); setError(""); try { await request(ciPath(`/notifications/${id}/read`), {}); await inbox.refetch(); } catch (e) { setError(String(e)); } finally { setBusy(false); } }
  return <main className="max-w-5xl mx-auto p-8 space-y-6"><h1>我的站内通知与待办</h1><NightlyNavigation /><CIIdentity />
    {identity && <><p>未读 {inbox.data?.unread_count ?? "…"} · 读通知不会关闭事件。</p><Button onClick={() => inbox.refetch()}>刷新通知</Button><label><input type="checkbox" checked={unreadOnly} onChange={e => setUnreadOnly(e.target.checked)} /> 仅未读</label>
      {(error || inbox.error) && <p role="alert">{error || String(inbox.error)}</p>}
      {inbox.data?.items.map(n => <article className="border p-4 space-y-2" key={n.notification_id}><h2>{n.kind} · {n.read_at ? "已读" : "未读"}</h2><p>实际记录 {n.created_at} · {n.batch_status === "COMPLETED" ? "本人当晚冻结数量" : "本人范围增量数量"}：完成 {n.total} / 原始失败 {n.original_failures} · 当前未关闭 {n.current_todos.length}</p>
        <Link className="underline" href={`/ci-incidents/nightly/${n.batch_id}`}>查看授权批次及关联视图</Link>{!n.read_at && <Button disabled={busy} onClick={() => read(n.notification_id)}>标为已读</Button>}
        {n.current_todos.map(t => <p key={t.incident_id}><Link className="underline" href={`/ci-incidents/${t.incident_id}`}>{t.incident_id} · {t.status}</Link></p>)}</article>)}
      <section><h2>当前本人未关闭待办</h2>{inbox.data?.current_todos.map(t => <p key={t.incident_id}><Link className="underline" href={`/ci-incidents/${t.incident_id}`}>{t.title} · {t.status}</Link></p>)}</section></>}
  </main>;
}
