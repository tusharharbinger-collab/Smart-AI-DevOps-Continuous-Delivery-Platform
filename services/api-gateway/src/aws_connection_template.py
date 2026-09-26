"""
services/api-gateway/src/aws_connection_template.py

Backlog #3 - the CloudFormation template a customer runs IN THEIR OWN AWS account to create the role this
platform assumes. Generated per connection so the platform's account id and that connection's ExternalId are
pre-filled (the customer never types either, so neither can be typo'd or chosen weakly).

Design (AWS third-party-access guidance; the same shape SaaS vendors such as Datadog use):
  * Trust policy: only the platform account, and only with `sts:ExternalId` equal to this connection's value.
  * MaxSessionDuration 1h; the platform requests 15-minute sessions.
  * Permissions cover exactly what the platform's generated infrastructure needs - no `Action: *`, and
    resource-level scoping wherever AWS lets a name/ARN pattern be expressed before the resource exists:
      - CloudFormation only on stacks named `smartcd-infra-*`
      - IAM role management only on roles named `smartcd-*`, and PassRole only to the services that need it
      - S3 only on buckets named `smartcd-*`
    ECS / ELB / RDS / ElastiCache / EC2 security-group create-time actions cannot be scoped by resource name
    (the resource has no ARN yet), so they are service-scoped instead. That is a real tradeoff, stated in the
    template's own Description so the customer can review it before creating the role.
  * It is checked by the platform's own policy engine in the test suite (no wildcard-admin statements).
"""
import json

ROLE_NAME = "smartcd-platform-access"

# What the platform's generated CloudFormation stacks create (see infra_generator.py's archetypes): an ALB +
# target group + listener, an ECS Fargate cluster/service/task definition, security groups, and optionally
# RDS and ElastiCache. Discovery and pricing need read access.
_READ_ONLY = [
    "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents", "cloudformation:DescribeStackResources",
    "cloudformation:DescribeChangeSet", "cloudformation:ListStacks", "cloudformation:GetTemplateSummary",
    "cloudformation:ValidateTemplate",
    "ecs:Describe*", "ecs:List*",
    "rds:Describe*", "rds:ListTagsForResource",
    "elasticache:Describe*", "elasticache:ListTagsForResource",
    "elasticloadbalancing:Describe*",
    "ec2:Describe*",
    "logs:DescribeLogGroups",
]

_CFN_STACK_ACTIONS = [
    "cloudformation:CreateChangeSet", "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
    "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
    "cloudformation:GetTemplate", "cloudformation:SetStackPolicy",
]

_SERVICE_ACTIONS = [
    "ecs:*", "elasticloadbalancing:*", "rds:*", "elasticache:*", "application-autoscaling:*",
    "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress",
    "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
    "ec2:CreateTags", "ec2:DeleteTags",
    "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:TagResource",
]

_IAM_ROLE_ACTIONS = [
    "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UpdateAssumeRolePolicy",
    "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
]

_S3_ACTIONS = [
    "s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging",
    "s3:PutEncryptionConfiguration", "s3:PutBucketVersioning", "s3:GetBucketLocation", "s3:ListBucket",
]


def build_role_template(platform_account_id: str, external_id: str, role_name: str = ROLE_NAME) -> dict:
    if not platform_account_id.isdigit() or len(platform_account_id) != 12:
        raise ValueError("platform_account_id must be a 12-digit AWS account id")
    if len(external_id) < 32:
        raise ValueError("external_id must be a high-entropy value of at least 32 characters")

    role_arn_prefix = {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/smartcd-*"}
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "ReadOnlyDiscoveryAndStatus", "Effect": "Allow", "Action": _READ_ONLY, "Resource": "*"},
            {
                "Sid": "ManageOnlyPlatformStacks", "Effect": "Allow", "Action": _CFN_STACK_ACTIONS,
                "Resource": {"Fn::Sub": "arn:${AWS::Partition}:cloudformation:*:${AWS::AccountId}:stack/smartcd-infra-*/*"},
            },
            {"Sid": "CreateTheInfrastructureServices", "Effect": "Allow", "Action": _SERVICE_ACTIONS, "Resource": "*"},
            {"Sid": "ManageOnlyPlatformRoles", "Effect": "Allow", "Action": _IAM_ROLE_ACTIONS, "Resource": role_arn_prefix},
            {
                "Sid": "PassRolesOnlyToTheServicesThatNeedThem", "Effect": "Allow", "Action": "iam:PassRole",
                "Resource": role_arn_prefix,
                "Condition": {"StringEquals": {"iam:PassedToService": ["ecs-tasks.amazonaws.com", "ecs.amazonaws.com", "rds.amazonaws.com"]}},
            },
            {
                "Sid": "ServiceLinkedRolesTheServicesNeed", "Effect": "Allow", "Action": "iam:CreateServiceLinkedRole",
                "Resource": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/aws-service-role/*"},
                "Condition": {"StringEquals": {"iam:AWSServiceName": [
                    "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com", "rds.amazonaws.com",
                    "elasticache.amazonaws.com", "ecs.application-autoscaling.amazonaws.com"]}},
            },
            {
                "Sid": "ManageOnlyPlatformBuckets", "Effect": "Allow", "Action": _S3_ACTIONS,
                "Resource": [{"Fn::Sub": "arn:${AWS::Partition}:s3:::smartcd-*"}],
            },
        ],
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "Lets the Smart AI DevOps platform provision infrastructure in THIS account. The role can only be assumed "
            "by the platform's AWS account (" + platform_account_id + ") and only with this connection's ExternalId. "
            "Permissions: read access for discovery/status; CloudFormation only on stacks named smartcd-infra-*; IAM "
            "role management only for roles named smartcd-*; S3 only for buckets named smartcd-*; and service-level "
            "create permissions for ECS, ELB, RDS and ElastiCache, which AWS cannot scope by resource name before the "
            "resource exists. Delete this stack to revoke all access."
        ),
        "Resources": {
            "PlatformAccessRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": role_name,
                    "MaxSessionDuration": 3600,
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"AWS": f"arn:aws:iam::{platform_account_id}:root"},
                            "Action": "sts:AssumeRole",
                            "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
                        }],
                    },
                    "Policies": [{"PolicyName": "smartcd-provisioning", "PolicyDocument": policy}],
                },
            }
        },
        "Outputs": {
            "RoleArn": {
                "Description": "Paste this into the platform to finish connecting the account.",
                "Value": {"Fn::GetAtt": ["PlatformAccessRole", "Arn"]},
            }
        },
    }


def build_role_template_json(platform_account_id: str, external_id: str) -> str:
    return json.dumps(build_role_template(platform_account_id, external_id), indent=2)
