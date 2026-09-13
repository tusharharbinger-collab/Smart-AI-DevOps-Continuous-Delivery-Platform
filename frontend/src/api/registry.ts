/**
 * frontend/src/api/registry.ts
 *
 * Registry credentials for the wizard's "Existing Image" tab (Phase 8
 * follow-up) — lets a project deploy a pre-built image straight into the
 * canary loop, skipping clone/build/test, the way Render's "Existing
 * Image" source type works. A private registry needs a pull credential;
 * this is a thin client for storing one server-side (Redis, tenant-scoped)
 * and referencing it by id — the raw secret is never sent again after the
 * call that creates it.
 */
import { apiClient } from "@/api/client";

export interface RegistryCredential {
  id: string;
  name: string;
  registry: string;
  username: string;
}

export const listRegistryCredentials = () =>
  apiClient.get<{ credentials: RegistryCredential[] }>("/api/v1/integrations/registry/credentials");

export const createRegistryCredential = (input: {
  name: string;
  registry: string;
  username: string;
  secret: string;
}) => apiClient.post<RegistryCredential>("/api/v1/integrations/registry/credentials", input);

export const deleteRegistryCredential = (id: string) =>
  apiClient.del<{ deleted: string }>(`/api/v1/integrations/registry/credentials/${id}`);

/**
 * Splits "docker.io/library/nginx:latest" into { image: "docker.io/library/nginx", tag: "latest" }.
 * The colon-vs-port ambiguity (a registry host can itself carry a port, e.g.
 * "registry.internal:5000/app:v1") is resolved by only treating a colon
 * AFTER the last "/" as a tag separator.
 */
export function parseImageRef(ref: string): { image: string; tag: string | null } {
  const trimmed = ref.trim();
  const lastSlash = trimmed.lastIndexOf("/");
  const lastColon = trimmed.lastIndexOf(":");
  if (lastColon > lastSlash) {
    return { image: trimmed.slice(0, lastColon), tag: trimmed.slice(lastColon + 1) };
  }
  return { image: trimmed, tag: null };
}
