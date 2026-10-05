"use client";

import {
  createContext,
  useContext,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  productGet,
  productLogin,
  productPrincipal,
  type ProductPrincipal,
  type ProductProject,
} from "@/lib/api/product";
import {
  productToken,
  setProductToken,
  subscribeProductSession,
} from "@/lib/api/product-session";
import { extractErrorMessage } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";

const ProductContext = createContext<ProductPrincipal | null>(null);
export function useProductPrincipal() {
  return useContext(ProductContext);
}
export const productAreas = [
  "analytics",
  "datasets",
  "cases",
  "suites",
  "experiments",
  "runs",
  "results",
  "gates",
  "rules",
  "reviews",
  "calibrations",
  "drafts",
] as const;
const names: Record<string, string> = {
  analytics: "产品分析",
  datasets: "数据集",
  cases: "用例",
  suites: "套件",
  experiments: "实验",
  runs: "运行",
  results: "结果",
  gates: "发布门禁",
  rules: "在线评估",
  reviews: "人工评审",
  calibrations: "校准",
  drafts: "反馈草稿",
};

export function ProductShell({ children }: { children?: ReactNode }) {
  const params = useParams<{ orgId?: string; projectId?: string }>();
  const cache = useQueryClient();
  const [principal, setPrincipal] = useState<ProductPrincipal | null>(
    productPrincipal,
  );
  const token = useSyncExternalStore(
    subscribeProductSession,
    productToken,
    () => null,
  );
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const projects = useQuery({
    queryKey: queryKeys.product.projects(principal?.principal_id ?? ""),
    queryFn: () => productGet<{ items: ProductProject[] }>("projects"),
    enabled: !!principal && !!token,
    staleTime: 0,
    retry: false,
  });
  async function login() {
    setBusy(true);
    setError("");
    try {
      const p = await productLogin(password);
      cache.clear();
      setPrincipal(p);
      setPassword("");
    } catch (e) {
      setError(extractErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  function logout() {
    setProductToken(null);
    cache.clear();
    setPrincipal(null);
    setError("");
  }
  if (!principal || !token)
    return (
      <main className="mx-auto max-w-md space-y-4 p-8">
        <h1 className="text-xl">AgentEvalOps · Go 产品后台</h1>
        <p>受控开发用户登录</p>
        <p className="text-sm text-text-dim">
          由服务端授予项目权限。登录会话仅在当前页面内存保存，刷新后需重新登录。
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void login();
          }}
          className="space-y-3"
        >
          <label className="block">
            开发登录密码
            <input
              aria-label="开发登录密码"
              type="password"
              autoComplete="off"
              className="block w-full border p-2"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button disabled={busy} className="border px-4 py-2">
            登录
          </button>
        </form>
        {error && <p role="alert">{error}</p>}
      </main>
    );
  const project = projects.data?.items.find(
    (p) => p.id === params.projectId && p.organization_id === params.orgId,
  );
  if (!params.projectId)
    return (
      <ProductContext.Provider value={principal}>
        <main className="p-8 space-y-4">
          <h1>选择项目</h1>
          <button onClick={logout}>退出登录</button>
          {projects.error && (
            <p role="alert">{extractErrorMessage(projects.error)}</p>
          )}
          {projects.data?.items.map((p) => (
            <p key={p.id}>
              <Link href={`/org/${p.organization_id}/project/${p.id}`}>
                {p.name}
              </Link>
            </p>
          ))}
        </main>
      </ProductContext.Provider>
    );
  return (
    <ProductContext.Provider value={principal}>
      <div className="min-h-screen p-6 space-y-6">
        <header className="flex gap-4">
          <Link href="/">项目</Link>
          <strong>{project?.name ?? "正在验证项目权限"}</strong>
          <span>CONTROLLED_DEV_AUTH</span>
          <button onClick={logout}>退出登录</button>
        </header>
        <nav className="flex flex-wrap gap-4">
          <Link href={`/org/${params.orgId}/project/${params.projectId}`}>
            概览
          </Link>
          {productAreas.map((area) => (
            <Link
              key={area}
              href={`/org/${params.orgId}/project/${params.projectId}/product/${area}`}
            >
              {names[area]}
            </Link>
          ))}
        </nav>
        {project ? (
          children
        ) : (
          <p role="alert">
            {projects.isPending
              ? "正在读取可访问项目…"
              : "NOT_FOUND: 无权访问此项目"}
          </p>
        )}
      </div>
    </ProductContext.Provider>
  );
}
