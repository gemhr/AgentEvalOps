"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { useCIIdentity } from "@/lib/api/ci-incidents";
import { CIIdentity } from "./IncidentWorkbench";
import { NightlyNavigation } from "./NightlyWorkbench";
import IncidentGroups from "./IncidentGroups";
import { ciPath, request, type BatchDetail, type Step, type Failure, type IncidentGroup } from "@/lib/api/ci-nightly";

function Controls({ batch, refresh }: { batch: BatchDetail; refresh: () => Promise<unknown> }) {
  const [intent, setIntent] = useState<{ kind: "advance"; steps: Step[] } | { kind: "clock"; target_business_time: string } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const eligible = batch.versions.flatMap(v => v.next_steps.filter(s => s.kind === "START" ? Date.parse(batch.simulated_now) >= Date.parse(batch.scheduled_start) : Date.parse(batch.simulated_now) >= Date.parse(v.slots.find(slot => slot.case_id === s.case_id)?.due_at ?? "")));
  async function submit(next: NonNullable<typeof intent>) {
    const current = intent ?? next; setIntent(current); setBusy(true); setError("");
    try { const { kind, ...body } = current; await request(ciPath(`/nightly/batches/${batch.batch_id}/${kind}`), body); setIntent(null); await refresh(); }
    catch (e) { setError(String(e)); await refresh(); } finally { setBusy(false); }
  }
  const midnight = new Date(batch.scheduled_start).toISOString();
  const plusTwoHours = new Date(Date.parse(batch.simulated_now) + 2 * 60 * 60 * 1000).toISOString();
  return <section className="space-y-3 border p-4"><h2>显式演练控制</h2><div className="flex gap-3 flex-wrap">
    <Button disabled={busy || !!intent || Date.parse(batch.simulated_now) >= Date.parse(midnight)} onClick={() => submit({ kind: "clock", target_business_time: midnight })}>模拟推进至 00:00</Button>
    <Button disabled={busy || !!intent} onClick={() => submit({ kind: "clock", target_business_time: plusTwoHours })}>模拟时钟 +2 小时</Button>
    <Button disabled={busy || !!intent || !eligible.length} onClick={() => submit({ kind: "advance", steps: eligible })}>执行当前到期步骤集合</Button>
    {intent && <Button disabled={busy} onClick={() => submit(intent)}>重试相同目标</Button>}{intent && !busy && <Button onClick={() => setIntent(null)}>取消待重试请求</Button>}</div>
    <p>每次请求每环境最多一步。时钟推进不生成结果，版本结束后必须再次点击启动下一版本。</p>
    {batch.versions.filter(v => v.next_steps.length).map(v => <div key={v.version_execution_id}>{v.environment_id} · v{["A", "B", "C"][v.ordinal - 1]} · {v.software_version}{" "}<Button disabled={busy || !!intent || !eligible.some(s => s.version_execution_id === v.version_execution_id)} onClick={() => submit({ kind: "advance", steps: v.next_steps })}>{v.next_steps[0].kind === "START" ? "启动版本" : `完成 ${v.next_steps[0].case_id}`}</Button></div>)}
    {error && <p role="alert">{error}</p>}
  </section>;
}

export default function NightlyBatchDetail({ batchId }: { batchId: string }) {
  const identity = useCIIdentity(); const cache = useQueryClient();
  const scope = ["ci", identity?.tenant_id, identity?.user_id, "nightly", batchId];
  const batch = useQuery({ queryKey: [...scope, "detail"], queryFn: () => request<BatchDetail>(ciPath(`/nightly/batches/${batchId}`)), enabled: !!identity });
  const failures = useQuery({ queryKey: [...scope, "failures"], queryFn: () => request<{ items: Failure[]; provisional: boolean }>(ciPath(`/nightly/batches/${batchId}/failures`)), enabled: !!identity });
  const groups = useQuery({ queryKey: [...scope, "groups"], queryFn: () => request<{ items: IncidentGroup[] }>(ciPath(`/nightly/batches/${batchId}/groups`)), enabled: !!identity });
  const refresh = () => cache.invalidateQueries({ queryKey: scope });
  const data = batch.data;
  return <main className="max-w-6xl mx-auto p-8 space-y-6"><h1>夜间批次详情</h1><NightlyNavigation /><CIIdentity />
    {(batch.error || failures.error || groups.error) && <p role="alert">{String(batch.error || failures.error || groups.error)}</p>}
    {identity && data && <><h2>{data.batch_id} · {data.status}</h2><Button onClick={() => refresh()}>刷新业务视图</Button><p>{data.scope === "OWN_ONLY" ? "当前视图仅本人负责范围" : "运行管理摘要；其他负责人的详细证据仍无权读取"}</p>
      <p>模拟业务时间（+08:00）：{new Date(data.simulated_now).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })} · 实际创建记录时间：{data.created_at}</p>
      <p>{data.status === "COMPLETED" ? "当晚结果已封存" : "PROVISIONAL · 增量观察，非整轮结论"} · 完成 {data.provisional_progress.total} · PASS {data.provisional_progress.passed} · 原始 FAIL {data.provisional_progress.failed} · 当前未关闭 {data.current_todos.length}</p>
      {data.can_manage && data.status !== "COMPLETED" && <Controls key={`${identity.user_id}:${batchId}`} batch={data} refresh={refresh} />}
      <section><h2>环境、版本和用例进度</h2>{data.versions.map(v => <article className="border p-3 space-y-2" key={v.version_execution_id}><h3>{v.environment_id} · v{["A", "B", "C"][v.ordinal - 1]} · {v.software_version} · {v.status}</h3><p>负责人 {v.owner_user_id} · 版次 {v.version_execution_id}</p>
        {v.slots.map(s => <div key={s.case_id}>{s.case_id} · 模拟 {s.duration_minutes} 分钟 · {s.result?.outcome ?? "尚未完成"}{s.due_at && <p>计划到期：{s.due_at}</p>}{s.result && <p>执行 {s.result.execution_id} · 模拟完成 {s.result.business_completed_at} · 实际记录 {s.result.recorded_at}</p>}</div>)}</article>)}</section>
      <section><h2>增量故障与当前处理状态</h2>{failures.data?.items.map(f => <article className="border p-3" key={f.execution_id}><p>{f.environment_id} · {f.software_version} · {f.case_id} · {f.status}</p><p>第一失败阶段 {f.first_failure?.stage} · {f.first_failure?.error} · {f.grouping_status}</p><p>Incident {f.incident_id} · 负责人 {f.owner_user_id}</p>{f.can_open && <Link className="underline" href={`/ci-incidents/${f.incident_id}`}>查看原始证据与人工处理</Link>}</article>)}</section>
      {groups.data && <IncidentGroups key={identity.user_id} batchId={batchId} groups={groups.data.items} refresh={refresh} />}
      {data.frozen_summary && <section><h2>当晚封存结果</h2><p>总执行 {data.frozen_summary.total} · PASS {data.frozen_summary.passed} · 原始 FAIL {data.frozen_summary.failed}</p><p>digest: {data.sealed_digest}</p><p>后续修复、关联纠正和已读状态不修改当晚结果。</p></section>}
      <details><summary>冻结初始配置与用例</summary><pre className="whitespace-pre-wrap">{JSON.stringify(data.frozen_plan, null, 2)}</pre></details></>}
  </main>;
}
