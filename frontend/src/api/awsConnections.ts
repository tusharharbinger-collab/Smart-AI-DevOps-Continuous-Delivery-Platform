/**
 * frontend/src/api/awsConnections.ts
 *
 * Backlog #3 - bring-your-own-AWS-account. A tenant connects THEIR OWN AWS account through a cross-account IAM
 * role instead of sharing the platform's. Mirrors services/api-gateway/src/routers/aws_connections_router.py.
 *
 * Flow: create (the platform generates the ExternalId and a CloudFormation template) -> the customer runs that
 * template in their account -> verify (the platform really assumes the role) -> usable for infra drafts.
 */
import { apiClient } from "@/api/client";

export type AwsConnectionStatus = "PENDING" | "VERIFIED" | "FAILED";

export interface AwsConnection {
  connection_id: string;
  name: string;
  status: AwsConnectionStatus;
  status_reason: string | null;
  role_arn: string | null;
  aws_account_id: string | null;
  default_region: string;
  verified_at: string | null;
  created_at: string | null;
}

/** Returned by create/get only: the material the customer needs to create the role. */
export interface AwsConnectionSetup extends AwsConnection {
  /** Platform-generated and unguessable; the customer never chooses it. It lives in their role's trust policy. */
  external_id: string;
  role_name: string;
  cloudformation_template?: string;
  cli_command?: string;
}

const BASE = "/api/v1/integrations/aws/connections";

export const listAwsConnections = () => apiClient.get<AwsConnection[]>(BASE);

export const createAwsConnection = (name: string, defaultRegion: string) =>
  apiClient.post<AwsConnectionSetup>(BASE, { name, default_region: defaultRegion });

export const getAwsConnection = (id: string) => apiClient.get<AwsConnectionSetup>(`${BASE}/${id}`);

/** The platform really assumes the role with this connection's ExternalId. Only success makes it usable. */
export const verifyAwsConnection = (id: string, roleArn: string) =>
  apiClient.post<AwsConnection>(`${BASE}/${id}/verify`, { role_arn: roleArn });

export const deleteAwsConnection = (id: string) => apiClient.del<{ deleted: string }>(`${BASE}/${id}`);
