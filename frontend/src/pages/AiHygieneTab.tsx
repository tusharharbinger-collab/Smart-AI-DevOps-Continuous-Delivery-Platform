import { useAppContext } from "@/hooks/useAppContext";
import { LogHygieneCard } from "@/components/verification/LogHygieneCard";

export function AiHygieneTab() {
  const { projectId } = useAppContext();
  if (!projectId) {
    return <p className="text-sm text-muted-foreground">Select a project to inspect code and log hygiene.</p>;
  }
  return (
    <div className="space-y-4">
      <LogHygieneCard projectId={projectId} />
    </div>
  );
}
