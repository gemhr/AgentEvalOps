"use client";

import { OrganizationProvider } from "@/components/providers/OrganizationProvider";
import { AuthGuard } from "@/components/features/AuthGuard";
import { InvitationBanner } from "@/components/features/InvitationBanner";
import { SessionReplayController } from "@/components/providers/SessionReplayController";
import { Sidebar } from "@/components/features/Sidebar";
import { TopBar } from "@/components/features/TopBar";
import { GO_PRODUCT_ENABLED } from "@/lib/api/product-session";
import { ProductShell } from "@/components/features/product/ProductShell";
import { usePathname } from "next/navigation";

export default function OrgLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  if (GO_PRODUCT_ENABLED) {
    const supported =
      /^\/org\/[^/]+\/project\/[^/]+(?:\/(?:evaluations|product\/[^/]+))?\/?$/.test(
        pathname,
      );
    return (
      <ProductShell>
        {supported ? (
          children
        ) : (
          <p>
            TEMPORARY_LEGACY_CONSUMER：此页面仍属于 Python
            产品界面，请从项目导航进入 Go 产品页面。
          </p>
        )}
      </ProductShell>
    );
  }
  return (
    <OrganizationProvider>
      <AuthGuard>
        <SessionReplayController />
        <div className="flex h-screen overflow-hidden">
          <Sidebar />
          <div className="flex flex-1 flex-col min-w-0">
            <TopBar />
            <main className="flex-1 overflow-y-auto p-6">
              <InvitationBanner />
              {children}
            </main>
          </div>
        </div>
      </AuthGuard>
    </OrganizationProvider>
  );
}
