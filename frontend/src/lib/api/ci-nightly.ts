"use client";

import { ciPath, request } from "./ci-incidents";
export { ciPath, request };

export type EnvironmentChoice = { environment_id: string; profile_key: string; owner_user_id: string; duration_profile: string };
export type NightlyOptions = { environments: { environment_id: string; ip: string; profile_key: string }[]; profiles: string[]; versions: string[]; duration_profiles: Record<string, number>; owners: { user_id: string; display_name: string }[] };
export type PlanIntent = { request_id: string; business_date: string; plan_key: string; environments: EnvironmentChoice[] };
export type BatchList = { can_create: boolean; items: { batch_id: string; business_date: string; status: string; can_manage: boolean }[] };
export type Step = { version_execution_id: string; kind: "START" | "COMPLETE_CASE"; case_id?: string };
export type ExecutionFact = { execution_id: string; version_execution_id: string; environment_id: string; owner_user_id: string; software_version: string; case_id: string; outcome: string; incident_id: string | null; business_completed_at: string; recorded_at: string };
export type Summary = { total: number; passed: number; failed: number; items: ExecutionFact[] };
export type BatchDetail = { batch_id: string; business_date: string; status: string; scope: string; can_manage: boolean; simulated_now: string; scheduled_start: string; created_at: string; sealed_digest: string | null; frozen_summary: Summary | null; provisional_progress: Summary; frozen_plan: unknown; current_todos: { incident_id: string; status: string }[]; versions: { version_execution_id: string; environment_id: string; owner_user_id: string; ordinal: number; software_version: string; status: string; started_business_at: string | null; completed_business_at: string | null; next_steps: Step[]; slots: { case_id: string; duration_minutes: number; due_at: string | null; result: ExecutionFact | null }[] }[] };
export type Failure = ExecutionFact & { status: string; first_failure: { stage: string; error: string } | null; grouping_status: string; can_open: boolean; evidence_refs: { evidence_id: string; kind: string; digest: string }[] };
export type IncidentGroup = { group_id: string; kind: string; revision: number; state: string; scope: string; source: string; signature: unknown; history: unknown[]; members: { incident_id: string; execution_id: string; environment_id: string; software_version: string; owner_user_id: string; decision: string; status: string; can_open: boolean }[] };
export type DecisionIntent = { request_id: string; expected_group_revision: number; kind: "CONFIRM" | "EXCLUDE" | "SPLIT"; incident_ids: string[]; reason: string };
export type Inbox = { unread_count: number; current_todos: { incident_id: string; title: string; status: string }[]; items: { notification_id: string; batch_id: string; kind: string; created_at: string; read_at: string | null; batch_status: string; total: number; original_failures: number; current_todos: { incident_id: string; status: string }[] }[] };
