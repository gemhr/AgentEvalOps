import type { NextConfig } from "next";

const securityHeaders = [
  { key: "X-Frame-Options", value: "SAMEORIGIN" },
  { key: "Content-Security-Policy", value: "frame-ancestors 'self'" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains; preload",
  },
];

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    const upstream = (process.env.LOCAL_AGENT_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    return [
      { source: "/api/agentcore/feature-impact/:path*", destination: `${upstream}/api/feature-impact/:path*` },
      { source: "/api/agentcore/ci/:path*", destination: `${upstream}/api/ci/:path*` },
      { source: "/api/agentcore/principal", destination: `${upstream}/api/v1/principal` },
      { source: "/api/agentcore/runs/:runId/events", destination: `${upstream}/api/v1/runs/:runId/events` },
    ];
  },
  async headers() {
    return [{ source: "/(.*)", headers: securityHeaders }];
  },
};

export default nextConfig;
