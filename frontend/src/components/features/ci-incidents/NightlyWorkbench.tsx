"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { CIIdentity } from "./IncidentWorkbench";
import { useCIIdentity } from "@/lib/api/ci-incidents";
import { ciPath, request, type BatchList, type NightlyOptions, type EnvironmentChoice, type PlanIntent } from "@/lib/api/ci-nightly";

export function NightlyNavigation() {
  return <nav className="flex gap-4"><Link href="/ci-incidents">故障工作台</Link><Link href="/ci-incidents/nightly">夜间 CI</Link><Link href="/ci-incidents/notifications">我的通知</Link></nav>;
}

function PlanForm({ options }: { options: NightlyOptions }) {
  const router = useRouter();
  const [businessDate, setBusinessDate] = useState(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date()));
  const [choices, setChoices] = useState<EnvironmentChoice[]>(options.environments.map((e, i) => ({ environment_id: e.environment_id, profile_key: e.profile_key, owner_user_id: options.owners[i % 2]?.user_id ?? "", duration_profile: "normal" })));
  const [count, setCount] = useState(6); const [intent, setIntent] = useState<PlanIntent | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  function change(index: number, field: keyof EnvironmentChoice, value: string) { setChoices(choices.map((c, i) => i === index ? { ...c, [field]: value } : c)); }
  async function create() {
    const body = intent ?? { request_id: crypto.randomUUID(), business_date: businessDate, plan_key: "small-nightly", environments: choices.slice(0, count) };
    setIntent(body); setBusy(true); setError("");
    try { const result = await request<{ batch_id: string }>(ciPath("/nightly/batches"), body); setIntent(null); router.push(`/ci-incidents/nightly/${result.batch_id}`); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <section className="space-y-3 border rounded p-4"><h2>创建小计划</h2><p>每环境依次执行 {options.versions.join(" → ")}，每版两个用例。选择两名有效 HUMAN 负责人；创建不会产生已完成执行。</p>
    <fieldset disabled={busy || !!intent} className="space-y-3"><label>业务日期 <input aria-label="业务日期" type="date" value={businessDate} onChange={e => setBusinessDate(e.target.value)} /></label>{" "}<label>环境数量 <select aria-label="环境数量" value={count} onChange={e => setCount(Number(e.target.value))}>{[4, 5, 6].map(n => <option key={n}>{n}</option>)}</select></label>
      {choices.slice(0, count).map((c, index) => <div className="flex flex-wrap gap-3" key={c.environment_id}><span>{c.environment_id} · {options.environments[index].ip}</span>
        <label>配置 <select aria-label={`${c.environment_id} 配置`} value={c.profile_key} onChange={e => change(index, "profile_key", e.target.value)}>{options.profiles.map(p => <option key={p}>{p}</option>)}</select></label>
        <label>负责人 <select aria-label={`${c.environment_id} 负责人`} value={c.owner_user_id} onChange={e => change(index, "owner_user_id", e.target.value)}>{options.owners.map(o => <option key={o.user_id} value={o.user_id}>{o.display_name}</option>)}</select></label>
        <label>每例模拟耗时 <select aria-label={`${c.environment_id} 耗时`} value={c.duration_profile} onChange={e => change(index, "duration_profile", e.target.value)}>{Object.entries(options.duration_profiles).map(([key, minutes]) => <option key={key} value={key}>{minutes} 分钟</option>)}</select></label></div>)}
    </fieldset><Button disabled={busy || options.owners.length < 2} onClick={create}>{intent ? "重试创建本计划" : "创建夜间批次"}</Button>{intent && !busy && <Button onClick={() => { setIntent(null); setError(""); }}>放弃本次创建意图</Button>}{error && <p role="alert">{error}</p>}
  </section>;
}

export default function NightlyWorkbench() {
  const identity = useCIIdentity(); const scope = ["ci", identity?.tenant_id, identity?.user_id, "nightly"];
  const batches = useQuery({ queryKey: [...scope, "list"], queryFn: () => request<BatchList>(ciPath("/nightly/batches")), enabled: !!identity });
  const options = useQuery({ queryKey: [...scope, "options"], queryFn: () => request<NightlyOptions>(ciPath("/nightly/options")), enabled: !!identity && !!batches.data?.can_create });
  return <main className="max-w-6xl mx-auto p-8 space-y-6"><h1>夜间 CI 业务演练</h1><NightlyNavigation /><p>显式模拟时钟和请求推进；服务停机不会继续执行。每个用例保留独立可修复副本，不是无人值守调度器。</p><CIIdentity />
    {identity && <>{options.data && <PlanForm key={identity.user_id} options={options.data} />}{(batches.error || options.error) && <p role="alert">{String(batches.error || options.error)}</p>}
      <section><h2>可见批次</h2>{batches.data?.items.map(b => <article className="border p-3" key={b.batch_id}><Link className="underline" href={`/ci-incidents/nightly/${b.batch_id}`}>{b.business_date} · {b.status}</Link><p>{b.batch_id} · {b.can_manage ? "本人控制" : "仅本人负责范围"}</p></article>)}</section></>}
  </main>;
}
