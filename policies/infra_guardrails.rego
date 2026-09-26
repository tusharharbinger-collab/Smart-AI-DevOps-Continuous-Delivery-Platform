# policies/infra_guardrails.rego
#
# AI_INFRA_PROVISIONING_EXECUTION_PLAN.md 7A / Phase 7f - the INDEPENDENT check on an AI-generated
# infrastructure proposal. Until now the "policy checks" a human saw were written by the same model
# that wrote the infrastructure (it graded its own homework). These rules run against the actual
# CloudFormation template - the artifact that really gets deployed - never against the model's
# description of it.
#
# Input:
#   template                     parsed CloudFormation template (object with .Resources)
#   intent_spec                  the human's locked requirements (tier, public_facing, budget, ...)
#   estimated_monthly_cost_usd   number
#   existing_resources           {slot: {...}} - resources the human attached instead of creating
#
# Findings are "deny" (a human cannot approve past it - edit the proposal) or "warn" (shown, does not
# block). A resource the human attached from their existing account is IMPORTED: CloudFormation cannot
# change its immutable properties (e.g. StorageEncrypted), so a problem there is a warning, not a
# blocker - but a NEW resource with the same problem is denied.
package infra.guardrails

import future.keywords.contains
import future.keywords.if
import future.keywords.in

resources := object.get(input.template, "Resources", {})

props(r) := object.get(r, "Properties", {})

truthy(v) if v == true

truthy(v) if v == "true"

has_existing if count(object.get(input, "existing_resources", {})) > 0

is_imported(r) if {
	has_existing
	r.DeletionPolicy == "Retain"
}

severity(r) := "warn" if is_imported(r)

else := "deny"

# ---------------------------------------------------------------- catalog
# `types` = CloudFormation types the rule applies to ([] = always applies).
catalog := {
	"rds_encryption_at_rest": {"description": "RDS storage is encrypted at rest", "types": ["AWS::RDS::DBInstance"]},
	"rds_not_public": {"description": "RDS is not publicly accessible", "types": ["AWS::RDS::DBInstance"]},
	"rds_no_plaintext_password": {"description": "No plaintext database master password in the template", "types": ["AWS::RDS::DBInstance"]},
	"rds_multi_az_in_production": {"description": "Production database is Multi-AZ", "types": ["AWS::RDS::DBInstance"]},
	"s3_not_public": {"description": "S3 buckets are not public", "types": ["AWS::S3::Bucket"]},
	"sg_no_open_admin_ports": {"description": "No 0.0.0.0/0 ingress except HTTP/HTTPS", "types": ["AWS::EC2::SecurityGroup"]},
	"lb_scheme_matches_intent": {"description": "Internet-facing load balancer only if the service is public", "types": ["AWS::ElasticLoadBalancingV2::LoadBalancer"]},
	"iam_no_wildcard_admin": {"description": "No IAM policy allowing Action * on everything", "types": ["AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"]},
	"imports_are_retained": {"description": "Attached existing resources are DeletionPolicy: Retain", "types": []},
	"within_budget": {"description": "Estimated monthly cost is within the declared budget", "types": []},
}

# ---------------------------------------------------------------- findings
finding(rule, id, sev, msg) := {"rule": rule, "resource": id, "severity": sev, "message": msg}

findings contains finding("rds_encryption_at_rest", id, severity(r), sprintf("%s: StorageEncrypted is not enabled", [id])) if {
	some id, r in resources
	r.Type == "AWS::RDS::DBInstance"
	not truthy(object.get(props(r), "StorageEncrypted", false))
}

findings contains finding("rds_not_public", id, severity(r), sprintf("%s: PubliclyAccessible is true", [id])) if {
	some id, r in resources
	r.Type == "AWS::RDS::DBInstance"
	truthy(object.get(props(r), "PubliclyAccessible", false))
}

# A literal string is a plaintext secret; a Ref/Fn::GetAtt/dynamic reference is an object, or a
# "{{resolve:secretsmanager:...}}" dynamic reference string.
findings contains finding("rds_no_plaintext_password", id, "deny", sprintf("%s: MasterUserPassword is a plaintext literal - use a Secrets Manager reference or ManageMasterUserPassword", [id])) if {
	some id, r in resources
	r.Type == "AWS::RDS::DBInstance"
	not is_imported(r)
	pw := object.get(props(r), "MasterUserPassword", null)
	is_string(pw)
	not startswith(pw, "{{resolve:")
}

findings contains finding("rds_multi_az_in_production", id, "warn", sprintf("%s: production tier but MultiAZ is not enabled", [id])) if {
	some id, r in resources
	r.Type == "AWS::RDS::DBInstance"
	input.intent_spec.environment_tier == "production"
	not truthy(object.get(props(r), "MultiAZ", false))
}

findings contains finding("s3_not_public", id, "deny", sprintf("%s: bucket ACL is public", [id])) if {
	some id, r in resources
	r.Type == "AWS::S3::Bucket"
	object.get(props(r), "AccessControl", "") in {"PublicRead", "PublicReadWrite", "AuthenticatedRead"}
}

findings contains finding("s3_not_public", id, "warn", sprintf("%s: no PublicAccessBlockConfiguration with all four blocks enabled", [id])) if {
	some id, r in resources
	r.Type == "AWS::S3::Bucket"
	not bucket_fully_blocked(r)
	not object.get(props(r), "AccessControl", "") in {"PublicRead", "PublicReadWrite", "AuthenticatedRead"}
}

bucket_fully_blocked(r) if {
	b := object.get(props(r), "PublicAccessBlockConfiguration", {})
	truthy(object.get(b, "BlockPublicAcls", false))
	truthy(object.get(b, "BlockPublicPolicy", false))
	truthy(object.get(b, "IgnorePublicAcls", false))
	truthy(object.get(b, "RestrictPublicBuckets", false))
}

open_cidrs := {"0.0.0.0/0", "::/0"}

findings contains finding("sg_no_open_admin_ports", id, "deny", sprintf("%s: ingress open to the internet on %s", [id, port_desc(rule)])) if {
	some id, r in resources
	r.Type == "AWS::EC2::SecurityGroup"
	some rule in object.get(props(r), "SecurityGroupIngress", [])
	rule_is_open(rule)
	not web_port_only(rule)
}

rule_is_open(rule) if rule.CidrIp in open_cidrs

rule_is_open(rule) if rule.CidrIpv6 in open_cidrs

web_port_only(rule) if {
	rule.FromPort == rule.ToPort
	rule.FromPort in {80, 443}
}

port_desc(rule) := sprintf("port %v-%v (%v)", [rule.FromPort, rule.ToPort, rule.IpProtocol]) if {
	rule.FromPort
} else := sprintf("all ports (%v)", [object.get(rule, "IpProtocol", "?")])

findings contains finding("lb_scheme_matches_intent", id, "deny", sprintf("%s: internet-facing load balancer for a service declared not public-facing", [id])) if {
	some id, r in resources
	r.Type == "AWS::ElasticLoadBalancingV2::LoadBalancer"
	object.get(props(r), "Scheme", "internet-facing") == "internet-facing"
	input.intent_spec.public_facing == false
}

findings contains finding("iam_no_wildcard_admin", id, "deny", sprintf("%s: IAM statement allows Action '*' on Resource '*'", [id])) if {
	some id, r in resources
	r.Type in {"AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}
	some doc in policy_documents(r)
	some stmt in as_array(object.get(doc, "Statement", []))
	stmt.Effect == "Allow"
	star(stmt.Action)
	star(object.get(stmt, "Resource", []))
}

policy_documents(r) := docs if {
	inline := [p.PolicyDocument | some p in object.get(props(r), "Policies", [])]
	own := [d | d := object.get(props(r), "PolicyDocument", null); d != null]
	docs := array.concat(inline, own)
}

as_array(x) := x if is_array(x)

else := [x]

star(x) if x == "*"

star(x) if {
	is_array(x)
	"*" in x
}

# Belt and braces with infra_generator.py's own validator: attached resources are always Retain.
slot_types := {
	"database": "AWS::RDS::DBInstance",
	"cache": "AWS::ElastiCache::CacheCluster",
	"ecs_cluster": "AWS::ECS::Cluster",
	"load_balancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
}

findings contains finding("imports_are_retained", id, "deny", sprintf("%s: represents an attached existing %s but is not DeletionPolicy: Retain", [id, slot])) if {
	some slot, _ in object.get(input, "existing_resources", {})
	some id, r in resources
	r.Type == slot_types[slot]
	# object.get: an ABSENT DeletionPolicy must count as "not Retain" (a bare `!=` on an undefined field is undefined).
	object.get(r, "DeletionPolicy", "") != "Retain"
}

findings contains finding("within_budget", "cost", "deny", sprintf("estimated $%.2f/mo exceeds the declared budget of $%.2f/mo", [input.estimated_monthly_cost_usd, input.intent_spec.monthly_budget_usd])) if {
	is_number(input.intent_spec.monthly_budget_usd)
	is_number(input.estimated_monthly_cost_usd)
	input.estimated_monthly_cost_usd > input.intent_spec.monthly_budget_usd
}

# ---------------------------------------------------------------- output
deny := {f | some f in findings; f.severity == "deny"}

warn := {f | some f in findings; f.severity == "warn"}

applicable(rule) if count(catalog[rule].types) == 0

applicable(rule) if {
	some r in resources
	r.Type in catalog[rule].types
}

check_status(rule) := "fail" if {
	some f in deny
	f.rule == rule
} else := "warn" if {
	some f in warn
	f.rule == rule
} else := "pass"

checks := [c |
	some rule, meta in catalog
	applicable(rule)
	c := {
		"check": rule,
		"description": meta.description,
		"status": check_status(rule),
		"details": [f.message | some f in findings; f.rule == rule],
	}
]

result := {
	"allowed": count(deny) == 0,
	"deny": deny,
	"warn": warn,
	"checks": checks,
}
