/**
 * frontend/src/api/services.ts
 *
 * Phase 3 (docs/roadmap/03-multi-service-onboarding.md): calls the real
 * `POST /api/v1/services`, which generates and applies real
 * Deployment/Service/HTTPRoute manifests to the cluster via pipeline-worker
 * and registers the resulting pipeline — the same one this same form's
 * "Confirm & Deploy" used to only pretend to create (see git history for
 * the Phase 1 mock this replaced).
 */
import { apiClient } from "@/api/client";

export interface RegisterServiceInput {
  name: string;
  image: string;
  baselineTag: string;
  canaryTag: string;
  port: number;
  healthCheckPath: string;
  trafficPathPrefix: string;
}

export interface RegisterServiceResult {
  pipeline_id: string;
  generatedPipelineYaml: string;
}

interface RegisterServiceResponse {
  pipeline_id: string;
  service_name: string;
  route_name: string;
  namespace: string;
  generated_pipeline_yaml: string;
}

export async function registerService(input: RegisterServiceInput): Promise<RegisterServiceResult> {
  const res = await apiClient.post<RegisterServiceResponse>("/api/v1/services", {
    service_name: input.name,
    image: input.image,
    baseline_tag: input.baselineTag,
    canary_tag: input.canaryTag,
    port: input.port,
    health_check_path: input.healthCheckPath,
    path_prefix: input.trafficPathPrefix,
  });
  return {
    pipeline_id: res.pipeline_id,
    generatedPipelineYaml: res.generated_pipeline_yaml,
  };
}
