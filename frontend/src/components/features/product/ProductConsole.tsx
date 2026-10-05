"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  productCommand,
  productGet,
  productPath,
  type ProductPage,
  type ProductRecord,
  type ReviewClaim,
  type ReviewView,
} from "@/lib/api/product";
import { extractErrorMessage } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useProductPrincipal } from "./ProductShell";

export function ProductFacts({ value }: { value: ProductRecord }) {
  const fields: Record<string, string> = {
    pipeline_state: "Pipeline State（流水线状态）",
    execution_outcome: "Execution Outcome（执行结果）",
    task_success: "Task Success（任务成功）",
    evaluation_verdict: "Evaluation Verdict（评估结论）",
    decision: "Gate Decision（门禁决策）",
    coverage: "Coverage（覆盖率、分母、缺失、未知、错误）",
    agreement:
      "Calibration（N、paired decidable、coverage、agreement、confusion）",
    protocol: "Blind Protocol（盲审协议）",
    sampling: "Sample Policy（抽样策略）",
    provider_identities: "Provider / Model（公开模型身份）",
    reason_codes: "Reason Codes（原因）",
    critical_regressions: "Critical Regressions（关键回退）",
    comparison_version: "Comparison Identity（比较身份）",
    baseline: "Baseline（基线）",
    candidate: "Candidate（候选）",
    policy_ref: "PolicyVersion（策略版本）",
    accepted_exceptions: "Exceptions（例外证明）",
    summary: "摘要",
    body: "发布内容",
    state: "派生状态",
    name: "名称",
  };
  const decision = value.decision;
  return (
    <div className="space-y-3">
      {typeof decision === "string" && (
        <p
          className={
            decision === "BLOCKED"
              ? "text-amber-400"
              : decision === "FAIL"
                ? "text-red-400"
                : "text-green-400"
          }
          data-testid="gate-decision"
        >
          Gate Decision: {decision}
          {decision === "BLOCKED" && " · 证据或可比性不足"}
          {decision === "FAIL" && " · 已证实回退"}
        </p>
      )}
      {Object.entries(fields)
        .filter(([field]) => value[field] !== undefined)
        .map(([field, label]) => (
          <section key={field}>
            <h3 className="font-bold">{label}</h3>
            <pre className="overflow-auto whitespace-pre-wrap text-sm">
              {JSON.stringify(value[field], null, 2)}
            </pre>
          </section>
        ))}
      {Array.isArray(value.attempts) && (
        <section>
          <h3>Execution Outcome（每次执行结果）</h3>
          {value.attempts.map((attempt, i) => (
            <ProductFacts key={i} value={attempt as ProductRecord} />
          ))}
        </section>
      )}
      {Array.isArray(value.results) && (
        <section>
          <h3>Task Success / Evaluation Verdict（独立展示）</h3>
          {value.results.map((result, i) => (
            <ProductFacts key={i} value={result as ProductRecord} />
          ))}
        </section>
      )}
    </div>
  );
}

export function ProductConsole({ area = "overview" }: { area?: string }) {
  const params = useParams<{ projectId: string }>();
  const principal = useProductPrincipal();
  const [cursor, setCursor] = useState<string | null>(null);
  const [id, setID] = useState("");
  const path = productPath(
    params.projectId,
    area + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""),
  );
  const list = useQuery({
    queryKey: queryKeys.product.resource(
      principal?.principal_id ?? "",
      params.projectId,
      path,
    ),
    queryFn: () => productGet<ProductPage & ProductRecord>(path),
    enabled: !!principal && area !== "results",
    staleTime: 0,
    refetchInterval: ["runs", "rules", "reviews"].includes(area)
      ? 10000
      : false,
    retry: false,
  });
  const detailPath = productPath(
    params.projectId,
    `${area}/${encodeURIComponent(id)}`,
  );
  const detail = useQuery({
    queryKey: queryKeys.product.resource(
      principal?.principal_id ?? "",
      params.projectId,
      detailPath,
    ),
    queryFn: () => productGet<ProductRecord>(detailPath),
    enabled: !!principal && !!id && area !== "reviews",
    staleTime: 0,
    retry: false,
  });
  return (
    <main className="space-y-5">
      <h1 className="text-xl">{area === "overview" ? "项目概览" : area}</h1>
      <p className="text-sm text-text-dim">
        执行结果、任务成功、评估结论、门禁决策分别读取；缺失证据不会表示为成功。
      </p>
      {list.error && <p role="alert">{extractErrorMessage(list.error)}</p>}
      {area === "overview" && list.data && !list.error && (
        <ProductFacts value={list.data} />
      )}
      {list.data?.items && !list.error && (
        <ul className="space-y-2">
          {list.data.items.map((row) => (
            <li key={String(row.id)}>
              <button
                className="underline"
                onClick={() => setID(String(row.id))}
              >
                {String(row.name ?? row.id)}
              </button>
              {typeof row.status === "string" && (
                <span> · {String(row.status)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {list.data?.next_cursor && (
        <button onClick={() => setCursor(list.data.next_cursor)}>下一页</button>
      )}
      {area !== "overview" && (
        <label className="block">
          资源 ID
          <input
            aria-label="资源 ID"
            className="ml-3 border p-2"
            value={id}
            onChange={(e) => setID(e.target.value)}
          />
        </label>
      )}
      {detail.error && <p role="alert">{extractErrorMessage(detail.error)}</p>}
      {detail.data && !detail.error && <ProductFacts value={detail.data} />}
      {area === "reviews" && id && (
        <ProductReview key={id} id={id} project={params.projectId} />
      )}
      {id && ["cases", "datasets", "suites"].includes(area) && (
        <ProductVersions area={area} id={id} project={params.projectId} />
      )}
      {area === "rules" && id && (
        <ProductRule key={id} project={params.projectId} id={id} />
      )}
      {area === "drafts" && id && (
        <ProductCommandForm
          project={params.projectId}
          path={`drafts/${id}/publish`}
          title="显式发布审核后的用例"
          initial='{"name":"审核后的用例"}'
        />
      )}
      {area === "experiments" && id && (
        <ProductCommandForm
          project={params.projectId}
          path={`experiments/${id}/materialize`}
          title="显式生成运行"
          initial="{}"
        />
      )}
      {[
        "cases",
        "datasets",
        "suites",
        "experiments",
        "runs",
        "gates",
        "rules",
        "calibrations",
        "drafts",
      ].includes(area) && (
        <ProductCommandForm
          project={params.projectId}
          path={area}
          title={
            area === "calibrations"
              ? "准备并生成校准报告"
              : area === "gates"
                ? "创建新的门禁决策"
                : `创建 ${area}`
          }
          initial={
            ["cases", "datasets", "suites", "rules"].includes(area)
              ? '{"name":"新资源"}'
              : "{}"
          }
        />
      )}
      {area === "drafts" && (
        <ProductCommandForm
          project={params.projectId}
          path="dataset-feedback"
          title="显式构建 Dataset Revision"
          initial='{"base":{"entity_id":"","version":""},"target":{"entity_id":"","version":""},"draft_ids":[]}'
        />
      )}
    </main>
  );
}

function ProductRule({ project, id }: { project: string; id: string }) {
  const [version, setVersion] = useState("");
  return (
    <section className="space-y-3">
      <ReadResource
        project={project}
        path={`rules/${id}/versions`}
        title="在线规则版本"
      />
      <ProductCommandForm
        project={project}
        path={`rules/${id}/versions`}
        title="显式发布在线规则版本"
        initial='{"version":"v1","enabled":true,"bindings":[]}'
      />
      <label>
        规则版本
        <input
          aria-label="规则版本"
          className="ml-3 border p-2"
          value={version}
          onChange={(e) => setVersion(e.target.value)}
        />
      </label>
      {version && (
        <ReadResource
          project={project}
          path={`rules/${id}/versions/${encodeURIComponent(version)}/coverage`}
          title="在线覆盖率：N / denominator / missing / unknown / error"
        />
      )}
      <ProductCommandForm
        project={project}
        path="online/backfills"
        title="显式创建回填任务"
        initial={JSON.stringify({
          rule: { entity_id: id, version },
          from: "",
          until: "",
          max_records: 100,
        })}
      />
      <ReadResource
        project={project}
        path="online/failure-candidates"
        title="失败候选"
      />
    </section>
  );
}
function ProductVersions({
  area,
  id,
  project,
}: {
  area: string;
  id: string;
  project: string;
}) {
  return (
    <section className="space-y-3">
      <ReadResource
        project={project}
        path={`${area}/${id}/versions`}
        title="不可变版本"
      />
      <ProductCommandForm
        project={project}
        path={`${area}/${id}/versions`}
        title="显式发布新版本"
        initial='{"version":"v1","body":{}}'
      />
    </section>
  );
}
function ReadResource({
  project,
  path,
  title,
}: {
  project: string;
  path: string;
  title: string;
}) {
  const p = useProductPrincipal();
  const fullPath = productPath(project, path);
  const query = useQuery({
    queryKey: queryKeys.product.resource(
      p?.principal_id ?? "",
      project,
      fullPath,
    ),
    queryFn: () => productGet<ProductRecord>(fullPath),
    retry: false,
    staleTime: 0,
  });
  return (
    <section>
      <h2>{title}</h2>
      {query.error && <p role="alert">{extractErrorMessage(query.error)}</p>}
      {query.data && !query.error && (
        <pre className="overflow-auto whitespace-pre-wrap text-sm">
          {JSON.stringify(query.data, null, 2)}
        </pre>
      )}
    </section>
  );
}
function ProductCommandForm({
  project,
  path,
  title,
  initial,
}: {
  project: string;
  path: string;
  title: string;
  initial: string;
}) {
  const cache = useQueryClient();
  const [body, setBody] = useState(initial);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [response, setResponse] = useState<ProductRecord | null>(null);
  async function submit() {
    setBusy(true);
    setError("");
    const commandKey = key || crypto.randomUUID();
    setKey(commandKey);
    try {
      const result = await productCommand<ProductRecord>(
        productPath(project, path),
        JSON.parse(body),
        commandKey,
      );
      setResponse(result);
      await cache.invalidateQueries({ queryKey: queryKeys.product.all });
    } catch (e) {
      setError(extractErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="border p-4">
      <summary>{title}</summary>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="block">
          命令内容
          <textarea
            aria-label={`${title}内容`}
            className="block w-full border p-2"
            rows={5}
            value={body}
            onChange={(e) => {
              setBody(e.target.value);
              setKey("");
              setResponse(null);
            }}
          />
        </label>
        <button className="border p-2" disabled={busy || !!response}>
          {title}
        </button>
        {error && <p role="alert">{error}</p>}
        {response && (
          <div>
            <p>
              命令已提交：{String(response.id ?? response.gate_id ?? "新版本")}
            </p>
            <ProductFacts value={response} />
          </div>
        )}
      </form>
    </details>
  );
}

function ProductReview({ id, project }: { id: string; project: string }) {
  const principal = useProductPrincipal();
  const cache = useQueryClient();
  const [claim, setClaim] = useState<ReviewClaim | null>(null);
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [viewAll, setViewAll] = useState(false);
  const [key, setKey] = useState("");
  const path = productPath(project, `reviews/${id}`);
  const query = useQuery({
    queryKey: queryKeys.product.resource(
      principal?.principal_id ?? "",
      project,
      path + (viewAll ? "/adjudication-view" : ""),
    ),
    queryFn: () =>
      productGet<ReviewView>(path + (viewAll ? "/adjudication-view" : "")),
    retry: false,
    staleTime: 0,
    refetchInterval: 10000,
  });
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const expired = !!claim && new Date(claim.lease).getTime() <= now;
  const item = query.error ? undefined : query.data;
  async function action(kind: "claim" | "renew" | "release" | "annotations") {
    setBusy(true);
    setError("");
    const commandKey = key || crypto.randomUUID();
    if (kind === "annotations") setKey(commandKey);
    try {
      const body =
        kind === "claim"
          ? { lease_seconds: 300 }
          : kind === "annotations"
            ? {
                slot: claim?.slot,
                token: claim?.token,
                decision: {
                  kind: item?.labels.includes("SUCCESS")
                    ? "TASK_SUCCESS"
                    : item?.labels.includes("TRUE_AGENT_FAILURE")
                      ? "FAILURE_CATEGORY"
                      : item?.labels.includes("TIE")
                        ? "PREFERENCE"
                        : "QUALITY_VERDICT",
                  value,
                  reason,
                  evidence_refs: item?.evidence.map((b) => b.ref),
                },
              }
            : { slot: claim?.slot, token: claim?.token, lease_seconds: 300 };
      const result = await productCommand<ReviewClaim>(
        `${path}/${kind}`,
        body,
        kind === "annotations" ? commandKey : undefined,
      );
      if (kind === "claim" || kind === "renew") setClaim(result);
      if (kind === "release") setClaim(null);
      if (kind === "annotations") {
        setSubmitted(true);
        setClaim(null);
      }
      await cache.invalidateQueries({ queryKey: queryKeys.product.all });
    } catch (e) {
      const message = extractErrorMessage(e);
      setError(message);
      if (message.startsWith("OWNERSHIP_LOST")) setClaim(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="space-y-3 border p-4">
      <h2>Review Detail（人工评审）</h2>
      {query.error && <p role="alert">{extractErrorMessage(query.error)}</p>}
      {item && (
        <>
          <p>状态：{item.status}</p>
          <p>
            {item.protocol.Blind
              ? "盲审：自动结论和其他评审决定由 API 隐藏"
              : "非盲审"}
          </p>
          <p>{item.reason}</p>
          <h3>证据</h3>
          {item.evidence.map((b) => (
            <p key={b.ref}>
              {b.availability}: {b.body ? JSON.stringify(b.body) : b.ref}
            </p>
          ))}
          {item.annotations && (
            <pre
              data-testid="adjudication-annotations"
              className="whitespace-pre-wrap"
            >
              {JSON.stringify(item.annotations, null, 2)}
            </pre>
          )}
          {submitted ||
          item.slots.some(
            (slot) => slot.mine && slot.status === "SUBMITTED",
          ) ? (
            <p>已经提交，本次决定不会再次提交。</p>
          ) : !claim ? (
            <button
              disabled={busy || !["PENDING", "IN_REVIEW"].includes(item.status)}
              onClick={() => void action("claim")}
            >
              领取评审
            </button>
          ) : (
            <>
              <p>
                {expired
                  ? "领取已过期：重新查看任务后再作决定"
                  : `领取剩余 ${Math.max(0, Math.ceil((new Date(claim.lease).getTime() - now) / 1000))} 秒`}
              </p>
              <button
                disabled={busy || expired}
                onClick={() => void action("renew")}
              >
                续期
              </button>
              <button disabled={busy} onClick={() => void action("release")}>
                释放
              </button>
              <label className="block">
                人工结论
                <select
                  aria-label="人工结论"
                  value={value}
                  onChange={(e) => {
                    setValue(e.target.value);
                    setKey("");
                  }}
                >
                  <option value="">选择</option>
                  {item.labels.map((label) => (
                    <option key={label}>{label}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                判断理由
                <input
                  aria-label="判断理由"
                  className="border p-2"
                  value={reason}
                  onChange={(e) => {
                    setReason(e.target.value);
                    setKey("");
                  }}
                />
              </label>
              <button
                disabled={busy || expired || !value || !reason}
                onClick={() => void action("annotations")}
              >
                提交独立判断
              </button>
            </>
          )}
          {["ADJUDICATION_REQUIRED", "COMPLETED"].includes(item.status) && (
            <>
              <button onClick={() => setViewAll(true)}>请求裁决视图</button>
              <ProductCommandForm
                project={project}
                path={`reviews/${id}/adjudications`}
                title="提交裁决"
                initial={JSON.stringify({
                  decision: {
                    kind: item.labels.includes("SUCCESS")
                      ? "TASK_SUCCESS"
                      : "QUALITY_VERDICT",
                    value: item.labels[0],
                    reason: "",
                    evidence_refs: item.evidence.map((b) => b.ref),
                  },
                  reason: "",
                })}
              />
              <ProductCommandForm
                project={project}
                path={`reviews/${id}/golden`}
                title="显式发布 GoldenLabel"
                initial="{}"
              />
            </>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
