import { extractErrorMessage, type ApiError } from "@/lib/api/client";
import axios from "axios";

describe("extractErrorMessage", () => {
  it("保留 Go 产品 API 的稳定错误类型", () => {
    for (const [status, code] of [
      [401, "UNAUTHENTICATED"],
      [403, "FORBIDDEN"],
      [404, "NOT_FOUND"],
      [409, "OWNERSHIP_LOST"],
      [412, "PRECONDITION_FAILED"],
      [429, "RATE_LIMITED"],
      [500, "INTERNAL"],
    ]) {
      const error = new axios.AxiosError("Request failed");
      Object.assign(error, {
        response: {
          status,
          data: { error: { code, message: "明确说明", request_id: "safe" } },
        },
      });
      expect(extractErrorMessage(error)).toBe(`${code}: 明确说明`);
    }
  });
  it("extracts detail from AxiosError response", () => {
    const error = new axios.AxiosError("Request failed");
    Object.assign(error, {
      response: { data: { detail: "Org not found" } as ApiError, status: 404 },
    });
    expect(extractErrorMessage(error)).toBe("Org not found");
  });

  it("falls back to error.message for AxiosError without detail", () => {
    const error = new axios.AxiosError("Network Error");
    expect(extractErrorMessage(error)).toBe("Network Error");
  });

  it("handles plain Error", () => {
    expect(extractErrorMessage(new Error("something broke"))).toBe(
      "something broke",
    );
  });

  it("returns default for unknown types", () => {
    expect(extractErrorMessage("string error")).toBe(
      "An unexpected error occurred",
    );
    expect(extractErrorMessage(null)).toBe("An unexpected error occurred");
    expect(extractErrorMessage(undefined)).toBe("An unexpected error occurred");
  });
});
