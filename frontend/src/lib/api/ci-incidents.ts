"use client";

import { useSyncExternalStore } from "react";

export type Principal = { user_id: string; tenant_id: string; principal_kind: string };
export type Scenario = { scenario_key: string; title: string; description: string };
export type Evidence = { evidence_id: string; kind: string; availability: string; digest: string | null; content?: unknown; missing_reason?: string | null };
export type Incident = { incident_id: string; title: string; status: string; execution_id: string; created_at: string };
export type InvestigationSummary = { investigation_id: string; run_id: string; report_version: number; investigation_status?: string; projection_status?: string };
export type StateChange = { path: string[]; before: unknown; after: unknown };
export type ActionOption = { action_kind: string; title: string; parameters: Record<string, unknown> };
export type CurrentState = { instance_id: string; state_revision: number; observed_at: string; environment: Record<string, unknown>; cases: { case_id: string; feature_id: string; input_revision: number }[]; report_field_revision: number; required_case_ids: string[]; allowed_actions: ActionOption[] };
export type ManualAction = { action_id: string; action_kind: string; source_kind: string; status: string; base_state_revision: number; applied_state_revision: number | null; stale: boolean; preview_digest: string; created_by_user_id: string; created_at: string; confirmed_by_user_id: string | null; confirmed_at: string | null; preview_payload: { description: string; environment_id: string; case_ids: string[]; changes: StateChange[] }; result_payload: unknown };
export type Verification = { verification_id: string; mode: string; state_revision: number; outcome: string; completed_at: string; created_by_user_id: string; items: { execution_id: string; case_id: string; state_revision: number; outcome: string; evidence: Evidence[] }[]; comparison: { original_execution_id: string; original_outcome: string; new_execution_id: string; new_outcome: string; changes: StateChange[] } };
export type CloseIntent = { request_id: string; expected_state_revision: number; action_id: string; single_verification_id: string; group_verification_id: string; root_cause: string; fix_confirmation: string; regression_confirmed: boolean };
export type Detail = Incident & { instance_id: string; state_revision: number; closed_at: string | null; closed_by_user_id: string | null; closure: { state_revision: number; source_kind: string; confirmation: CloseIntent } | null; actions: ManualAction[]; verifications: Verification[]; closure_eligibility: { eligible: boolean; reasons: string[]; action_id: string | null; single_verification_id: string | null; group_verification_id: string | null }; execution: { execution_id: string; outcome: string }; evidence: Evidence[]; investigations: InvestigationSummary[] };
export type Report = { diagnosis_status: string; failure_stage: string; root_cause_hypothesis: string; supporting_evidence: { evidence_id: string; digest: string }[]; uncertainties: string[]; recommended_checks: string[]; recommended_owner_role: string };
export type Investigation = InvestigationSummary & { investigation_status: string; runtime_status: string | null; diagnosis_status: string | null; safe_error_code: string | null; report: Report | null };
export type Start = InvestigationSummary & { events_url: string; report_url: string };

let token = "";
let principal: Principal | null = null;
const listeners = new Set<() => void>();
const subscriptions = new Set<AbortController>();
const notify = () => listeners.forEach((fn) => fn());
export function useCIIdentity() {
  return useSyncExternalStore((fn) => { listeners.add(fn); return () => { listeners.delete(fn); }; }, () => principal, () => null);
}
export function logout() {
  token = ""; principal = null;
  subscriptions.forEach((controller) => controller.abort()); subscriptions.clear(); notify();
}

export async function request<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  if (!path.startsWith("/api/agentcore/")) throw new Error("CI_API_PATH_INVALID");
  const credential = token;
  const response = await fetch(path, { method: body ? "POST" : "GET", cache: "no-store", signal,
    headers: { Authorization: `Bearer ${credential}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error(`请求失败 (${response.status}) ${(await response.text()).slice(0, 200)}`);
  const result = await response.json();
  if (credential !== token) throw new Error("CI_IDENTITY_CHANGED");
  return result;
}

export async function login(value: string) {
  logout(); token = value.trim();
  try {
    const { principal: verified } = await request<{ principal: Principal }>("/api/agentcore/principal");
    if (verified.principal_kind !== "HUMAN") throw new Error("需要 HUMAN 用户身份");
    principal = verified; notify(); return verified;
  } catch (error) { logout(); throw error; }
}

export const ciPath = (path: string) => `/api/agentcore/ci${path}`;
export const terminal = (value?: Investigation) => !!value && ["COMPLETED", "FAILED", "UNCONFIRMED"].includes(value.investigation_status);

export function subscribe(runId: string, onProgress: (kind: string) => void, onRefresh: () => void) {
  const controller = new AbortController(); subscriptions.add(controller);
  void (async () => {
    let cursor = "";
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(`/api/agentcore/runs/${encodeURIComponent(runId)}/events`, {
          headers: { Authorization: `Bearer ${token}`, ...(cursor ? { "Last-Event-ID": cursor } : {}) },
          signal: controller.signal, cache: "no-store" });
        if (!response.ok || !response.body) throw new Error("SSE_UNAVAILABLE");
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = "";
        try {
          while (!controller.signal.aborted) {
            const { value, done } = await reader.read(); if (done) break;
            pending += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
            let boundary: number;
            while ((boundary = pending.indexOf("\n\n")) >= 0) {
              const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
              const lines = frame.split("\n"); const id = lines.find((line) => line.startsWith("id:"));
              if (id) cursor = id.slice(3).trim();
              const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
              if (event) { onProgress(event); onRefresh(); }
            }
          }
        } finally { await reader.cancel(); }
      } catch { if (controller.signal.aborted) break; onProgress("连接中断，正在重新订阅"); }
      onRefresh();
      await new Promise<void>((resolve) => { const timer = setTimeout(resolve, 2000); controller.signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true }); });
    }
  })();
  return () => { controller.abort(); subscriptions.delete(controller); };
}
