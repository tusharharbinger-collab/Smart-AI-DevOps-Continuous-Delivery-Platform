import { API_BASE_URL, apiClient, authHeaders } from "./client";

export interface AuditEntry {
  actuation_id: string;
  timestamp: string;
  action: string;
  pipeline_run_id: string;
  verdict: string | null;
  confidence: number | null;
  authorized_by: string;
  hmac_signature: string;
}

export const getAuditLog = (tenantId?: string) =>
  apiClient.get<{ entries: AuditEntry[] }>(
    `/api/v1/audit${tenantId ? `?tenant_id=${tenantId}` : ""}`
  );

export async function exportSOC2(_tenantId: string) {
  // A plain `window.open`/`<a href>` link can't attach an Authorization
  // header, and the backend (auth/middleware.py) only ever reads the token
  // from that header — a `?auth=` query param is never read server-side, so
  // that approach always 401s regardless of what token is passed. Fetch the
  // CSV directly with the same auth header every other request uses, then
  // hand the browser a Blob URL to save.
  const res = await fetch(`${API_BASE_URL}/api/v1/audit/export/soc2`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`SOC 2 export failed: ${res.status} ${body}`);
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const filename =
    res.headers.get("content-disposition")?.match(/filename=([^;]+)/)?.[1]?.trim() ??
    "soc2_export.csv";

  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
