// 用户 Bearer 仅在内存保存；不写入 cookie、localStorage 或 bundle。
export const PRODUCT_API_URL = process.env.NEXT_PUBLIC_GO_PRODUCT_API_URL ?? "";
export const GO_PRODUCT_ENABLED = PRODUCT_API_URL.length > 0;
let accessToken: string | null = null;
const listeners = new Set<() => void>();
export function productToken() {
  return accessToken;
}
export function setProductToken(token: string | null) {
  accessToken = token;
  listeners.forEach((listener) => listener());
}
export function subscribeProductSession(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
export function isProductRequest(config: { baseURL?: string; url?: string }) {
  return (
    GO_PRODUCT_ENABLED &&
    config.baseURL === PRODUCT_API_URL &&
    config.url?.startsWith("/api/v1/")
  );
}
