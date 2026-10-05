"use client";

import { ProjectProvider } from "@/components/providers/ProjectProvider";
import { EvalRunTrackerProvider } from "@/components/providers/EvalRunTrackerProvider";
import { GO_PRODUCT_ENABLED } from "@/lib/api/product-session";

export default function ProjectLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  if (GO_PRODUCT_ENABLED) return <>{children}</>;
  return (
    <ProjectProvider>
      <EvalRunTrackerProvider>{children}</EvalRunTrackerProvider>
    </ProjectProvider>
  );
}
