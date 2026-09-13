import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";
import { getAuditLog, type AuditEntry } from "../api/audit";

/**
 * Audit entries for the current scope.
 *
 * `projectId` (Phase 8) narrows the ledger to one project's runs. Without
 * it this returns the whole tenant's ledger, which is correct for the
 * classic console but would leak every other project's actuations into a
 * project workspace that is supposed to be isolated.
 */
export function useAuditLog(tenantId?: string, projectId?: string) {
  const { data, isLoading } = useQuery({
    queryKey: ["audit-log", tenantId, projectId],
    queryFn: () =>
      projectId
        ? apiClient.get<{ entries: AuditEntry[] }>(`/api/v1/projects/${projectId}/audit`)
        : getAuditLog(tenantId),
    refetchInterval: 15000,
  });

  return { entries: data?.entries ?? [], isLoading };
}
