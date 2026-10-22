import NightlyBatchDetail from "@/components/features/ci-incidents/NightlyBatchDetail";
export default async function Page({ params }: { params: Promise<{ batchId: string }> }) {
  const { batchId } = await params;
  return <NightlyBatchDetail batchId={batchId} />;
}
