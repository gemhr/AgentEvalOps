import { client } from "./client";
import { PRODUCT_API_URL, setProductToken } from "./product-session";

export interface ProductPrincipal {
  principal_id: string;
  principal_type: string;
  organization_id: string;
  auth_method: string;
  capabilities: string[];
}
export interface ProductProject {
  id: string;
  organization_id: string;
  name: string;
}
export interface ProductPage {
  items: ProductRecord[];
  next_cursor: string | null;
}
export type ProductRecord = Record<string, unknown>;
export type CIBusinessResult = { result_id: string; run_id: string; case: { entity_id: string; version: string }; input: { records: { canonical_bytes_b64: string; sha256: string }[] }; reference: Record<string, unknown>; actual: Record<string, unknown>; provenance: Record<string, unknown>; score: number | null; verdict: string; reason: string };
export const createCIBusinessRun = (project: string, suite: { entity_id: string; version: string }, rule_version: string, command: string) => productCommand<{ id: string }>(productPath(project, "ci-business/runs"), { suite, rule_version }, command);
export const readCIBusinessResult = (project: string, result: string) => productGet<CIBusinessResult>(productPath(project, `results/${encodeURIComponent(result)}/business-quality`));
export interface ReviewClaim {
  item_id: string;
  slot: number;
  token: string;
  lease: string;
}
export interface ReviewView {
  id: string;
  status: string;
  reason: string;
  labels: string[];
  protocol: { Blind: boolean; JudgeShown: boolean };
  evidence: { ref: string; availability: string; body?: unknown }[];
  slots: {
    number: number;
    status: string;
    mine: boolean;
    token?: string;
    lease?: string;
  }[];
  annotations?: ProductRecord[];
}
const config = { baseURL: PRODUCT_API_URL };
let currentPrincipal: ProductPrincipal | null = null;
export function productPrincipal() {
  return currentPrincipal;
}
export async function productGet<T>(path: string): Promise<T> {
  return (await client.get<T>(`/api/v1/${path}`, config)).data;
}
export async function productCommand<T>(
  path: string,
  body: unknown,
  key?: string,
): Promise<T> {
  return (
    await client.post<T>(`/api/v1/${path}`, body, {
      ...config,
      headers: key ? { "Idempotency-Key": key } : {},
    })
  ).data;
}
export async function productLogin(password: string) {
  const result = await productCommand<{ access_token: string }>(
    "auth/dev-login",
    { password },
  );
  setProductToken(result.access_token);
  currentPrincipal = await productGet<ProductPrincipal>("me");
  return currentPrincipal;
}
export function productPath(project: string, resource: string) {
  return `projects/${encodeURIComponent(project)}/${resource}`;
}
