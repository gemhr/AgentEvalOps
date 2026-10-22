import IncidentDetail from "@/components/features/ci-incidents/IncidentDetail";
export default async function Page({ params }: { params: Promise<{ incidentId: string }> }) {
  const { incidentId } = await params;
  return <IncidentDetail incidentId={incidentId} />;
}
