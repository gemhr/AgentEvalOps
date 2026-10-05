"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { productGet, productPath, type ProductRecord } from "@/lib/api/product";
import { extractErrorMessage } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useProductPrincipal } from "./ProductShell";

export interface AnalyticsResponse {
  projection_version: string;
  as_of: string;
  timezone: string;
  query: { from: string; until: string; bucket: string; source_kind: string };
  unit: string;
  denominator: string;
  consistency: string;
  rows: ProductRecord[];
}
const sections: Record<string, string> = {
  overview: "项目计数",
  quality: "质量",
  trends: "趋势",
  gates: "门禁",
  online: "在线评估",
  human: "人工与校准",
};
const columns: Record<string, string[]> = {
  quality: [
    "subject",
    "subject_version",
    "dataset",
    "evaluator",
    "metric",
    "expected",
    "completed",
    "pass",
    "fail",
    "evaluation_inconclusive",
    "error",
    "evaluation_missing",
    "not_applicable",
    "success",
    "failure",
    "inconclusive",
    "unknown",
    "missing",
    "unsupported",
    "eligible",
    "decidable",
    "task_success_rate",
    "decision_coverage",
    "evaluation_coverage",
  ],
  gates: [
    "policy",
    "policy_digest",
    "baseline_kind",
    "candidate_kind",
    "receipts",
    "pass",
    "fail",
    "blocked",
    "critical_regressions",
    "coverage_blockers",
    "incomparables",
  ],
  online: [
    "rule_id",
    "rule_version",
    "source",
    "eligible",
    "sampled",
    "sampling_coverage",
    "admission_budget_skipped",
    "sampling",
    "filter",
  ],
  human: [
    "eligible",
    "assigned",
    "submitted",
    "adjudicated",
    "golden",
    "human_label_coverage",
    "calibration_report_id",
    "evaluator",
    "protocol",
    "agreement",
    "dataset",
  ],
};
export function analyticsValue(value: unknown, field: string) {
  if (value === null || value === undefined) return "不可判定 / N/A";
  if (
    typeof value === "number" &&
    (field.endsWith("coverage") || field.endsWith("rate"))
  )
    return `${(value * 100).toFixed(1)}%`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
export function AnalyticsTable({
  rows,
  fields,
}: {
  rows: ProductRecord[];
  fields: string[];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr>
            {fields.map((field) => (
              <th className="border-b p-2" key={field}>
                {field}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {fields.map((field) => (
                <td className="max-w-sm border-b p-2 break-words" key={field}>
                  {analyticsValue(row[field], field)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function ProductAnalytics({ overview = false }: { overview?: boolean }) {
  const { projectId } = useParams<{ projectId: string }>();
  const principal = useProductPrincipal();
  const [section, setSection] = useState("overview");
  const [window, setWindow] = useState("7d");
  const [bucket, setBucket] = useState("day");
  const [from, setFrom] = useState("");
  const [until, setUntil] = useState("");
  const [subject, setSubject] = useState("");
  const [evaluator, setEvaluator] = useState("");
  const parameters = new URLSearchParams({ window, bucket });
  if (window === "custom") {
    parameters.set("from", from);
    parameters.set("until", until);
  }
  if (["quality", "trends"].includes(section)) {
    if (subject) parameters.set("subject", subject);
    if (evaluator) parameters.set("evaluator", evaluator);
  }
  const resource = section === "overview" ? "overview" : `analytics/${section}`;
  const path = productPath(projectId, `${resource}?${parameters}`);
  const result = useQuery({
    queryKey: queryKeys.product.resource(
      principal?.principal_id ?? "",
      projectId,
      path,
    ),
    queryFn: () =>
      productGet<AnalyticsResponse & { analytics?: AnalyticsResponse }>(path),
    enabled: !!principal && (window !== "custom" || (!!from && !!until)),
    staleTime: 0,
    retry: false,
  });
  const data = result.data?.analytics ?? result.data;
  const fields =
    section === "overview"
      ? Object.keys(data?.rows[0] ?? {})
      : section === "trends"
        ? ["bucket", ...columns.quality]
        : columns[section];
  return (
    <main className="space-y-5">
      <h1 className="text-xl">{overview ? "项目概览" : "产品分析"}</h1>
      <nav className="flex flex-wrap gap-3" aria-label="分析视图">
        {Object.entries(sections).map(([key, name]) => (
          <button
            aria-pressed={section === key}
            key={key}
            onClick={() => setSection(key)}
          >
            {name}
          </button>
        ))}
      </nav>
      <div className="flex flex-wrap gap-3">
        <label>
          时间窗口{" "}
          <select
            aria-label="时间窗口"
            value={window}
            onChange={(e) => {
              setWindow(e.target.value);
              setBucket(e.target.value === "24h" ? "hour" : "day");
            }}
          >
            <option value="24h">24 小时</option>
            <option value="7d">7 天</option>
            <option value="30d">30 天</option>
            <option value="custom">自定义（最多 90 天）</option>
          </select>
        </label>
        <label>
          UTC 桶{" "}
          <select
            aria-label="UTC 桶"
            value={bucket}
            onChange={(e) => setBucket(e.target.value)}
          >
            <option value="hour">小时</option>
            <option value="day">天</option>
          </select>
        </label>
        {window === "custom" && (
          <>
            <label>
              开始 RFC3339{" "}
              <input
                aria-label="开始 RFC3339"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              />
            </label>
            <label>
              结束 RFC3339{" "}
              <input
                aria-label="结束 RFC3339"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
            </label>
          </>
        )}
        {["quality", "trends"].includes(section) && (
          <>
            <label>
              Subject{" "}
              <input
                aria-label="Subject"
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
              />
            </label>
            <label>
              Evaluator ID@version{" "}
              <input
                aria-label="Evaluator ID@version"
                value={evaluator}
                onChange={(e) => setEvaluator(e.target.value)}
              />
            </label>
          </>
        )}
        <button onClick={() => result.refetch()}>刷新</button>
      </div>
      {result.isFetching && <p role="status">正在读取分析事实…</p>}
      {result.error && (
        <p role="alert">
          {isAxiosError(result.error) &&
            `HTTP ${result.error.response?.status ?? "网络错误"} · `}
          {extractErrorMessage(result.error)}
        </p>
      )}
      {data && !result.error && (
        <>
          <p data-testid="analytics-as-of">
            截至 {data.as_of} · {data.timezone} · {data.projection_version}
          </p>
          <p>
            范围 [{data.query.from}, {data.query.until}) · {data.query.bucket} ·{" "}
            {data.query.source_kind}
          </p>
          <p>单位：{data.unit}</p>
          <p>分母：{data.denominator}</p>
          <p>
            不可判定 / N/A
            表示没有适用分母；在线指标仅描述实际抽样，不能外推全部生产流量。
          </p>
          {data.rows.length === 0 ? (
            <p>此范围没有事实。</p>
          ) : (
            <AnalyticsTable rows={data.rows} fields={fields} />
          )}
          {section === "online" &&
            data.rows.map(
              (row, i) =>
                Array.isArray(row.segments) && (
                  <section key={i}>
                    <h2>
                      Evaluator slot · {String(row.rule_id)}@
                      {String(row.rule_version)}
                    </h2>
                    <AnalyticsTable
                      rows={row.segments as ProductRecord[]}
                      fields={[
                        "evaluator_id",
                        "evaluator_version",
                        "metric_id",
                        "metric_version",
                        "work",
                        "completed",
                        "decidable",
                        "missing",
                        "budget_skipped",
                        "rate_limited",
                        "evaluation_coverage",
                        "decision_coverage",
                      ]}
                    />
                  </section>
                ),
            )}
        </>
      )}
    </main>
  );
}
