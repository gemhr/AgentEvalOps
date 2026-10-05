import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  ProductAnalytics,
  analyticsValue,
} from "@/components/features/product/ProductAnalytics";
import { productGet } from "@/lib/api/product";

jest.mock("next/navigation", () => ({ useParams: () => ({ projectId: "p" }) }));
jest.mock("@/components/features/product/ProductShell", () => ({
  useProductPrincipal: () => ({ principal_id: "u" }),
}));
jest.mock("@/lib/api/product", () => ({
  productGet: jest.fn(),
  productPath: (project: string, resource: string) =>
    `projects/${project}/${resource}`,
}));
jest.mock("@/lib/api/client", () => ({
  extractErrorMessage: (e: Error) => e.message,
}));

test("保留分母、版本身份和不可判定；质量查询使用项目上下文", async () => {
  expect(analyticsValue(null, "task_success_rate")).toBe("不可判定 / N/A");
  expect(analyticsValue(0, "task_success_rate")).toBe("0.0%");
  const response = {
    projection_version: "stage12.analytics.v1",
    as_of: "2026-10-05T00:00:00Z",
    timezone: "UTC",
    query: {
      from: "2026-09-28T00:00:00Z",
      until: "2026-10-05T00:00:00Z",
      bucket: "day",
      source_kind: "OFFLINE",
    },
    unit: "case-run",
    denominator: "decidable/eligible",
    rows: [
      {
        subject_version: "v1",
        evaluator: "e@v1",
        metric: "task_success.v1",
        expected: 10,
        eligible: 10,
        decidable: 0,
        task_success_rate: null,
      },
    ],
  };
  jest.mocked(productGet).mockResolvedValue(response);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ProductAnalytics />
    </QueryClientProvider>,
  );
  await screen.findByTestId("analytics-as-of");
  fireEvent.click(screen.getByRole("button", { name: "质量" }));
  await screen.findByText("e@v1");
  expect(screen.getByText("分母：decidable/eligible")).toBeVisible();
  expect(productGet).toHaveBeenCalledWith(
    "projects/p/analytics/quality?window=7d&bucket=day",
  );
  expect(screen.queryByText("0.0%")).not.toBeInTheDocument();
});

test("错误响应不显示上次的分析事实", async () => {
  jest.mocked(productGet).mockRejectedValue(new Error("资源不可访问"));
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ProductAnalytics />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("资源不可访问");
  expect(screen.queryByTestId("analytics-as-of")).not.toBeInTheDocument();
});
