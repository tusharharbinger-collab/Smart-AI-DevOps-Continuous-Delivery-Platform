package infra.guardrails_test

import data.infra.guardrails
import future.keywords.in

good_db := {"Type": "AWS::RDS::DBInstance", "Properties": {
	"StorageEncrypted": true, "PubliclyAccessible": false, "MultiAZ": true,
	"MasterUserPassword": "{{resolve:secretsmanager:db:SecretString:password}}",
}}

spec := {"environment_tier": "dev", "public_facing": true, "monthly_budget_usd": 100}

mk(resources, extra) := object.union(
	{"template": {"Resources": resources}, "intent_spec": spec, "estimated_monthly_cost_usd": 50, "existing_resources": {}},
	extra,
)

rules_denied(res) := {f.rule | some f in res.deny}

rules_warned(res) := {f.rule | some f in res.warn}

test_a_clean_proposal_is_allowed {
	res := guardrails.result with input as mk({"Db": good_db}, {})
	res.allowed
	count(res.deny) == 0
}

test_unencrypted_new_db_is_denied {
	res := guardrails.result with input as mk({"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": false}}}, {})
	not res.allowed
	"rds_encryption_at_rest" in rules_denied(res)
}

test_missing_encryption_property_is_denied {
	res := guardrails.result with input as mk({"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {}}}, {})
	"rds_encryption_at_rest" in rules_denied(res)
}

test_public_db_is_denied {
	res := guardrails.result with input as mk({"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": true, "PubliclyAccessible": true}}}, {})
	"rds_not_public" in rules_denied(res)
}

test_string_true_is_treated_as_true {
	res := guardrails.result with input as mk({"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": "true", "PubliclyAccessible": "true"}}}, {})
	not "rds_encryption_at_rest" in rules_denied(res)
	"rds_not_public" in rules_denied(res)
}

test_plaintext_master_password_is_denied {
	db := object.union(good_db, {"Properties": object.union(good_db.Properties, {"MasterUserPassword": "ChangeMe123!"})})
	res := guardrails.result with input as mk({"Db": db}, {})
	"rds_no_plaintext_password" in rules_denied(res)
}

test_secretsmanager_dynamic_reference_and_ref_are_fine {
	ref_db := object.union(good_db, {"Properties": object.union(good_db.Properties, {"MasterUserPassword": {"Ref": "DbPassword"}})})
	res := guardrails.result with input as mk({"Db": ref_db}, {})
	not "rds_no_plaintext_password" in rules_denied(res)
	res.allowed
}

test_an_imported_unencrypted_db_only_warns {
	# CloudFormation cannot change an imported resource's immutable properties, so blocking the human
	# from attaching their own (unencrypted) database would be wrong - warn, don't deny.
	db := {"Type": "AWS::RDS::DBInstance", "DeletionPolicy": "Retain", "Properties": {"StorageEncrypted": false}}
	res := guardrails.result with input as mk({"Db": db}, {"existing_resources": {"database": {"id": "orders-db"}}})
	res.allowed
	"rds_encryption_at_rest" in rules_warned(res)
}

test_production_without_multi_az_warns {
	db := object.union(good_db, {"Properties": object.union(good_db.Properties, {"MultiAZ": false})})
	res := guardrails.result with input as mk({"Db": db}, {"intent_spec": object.union(spec, {"environment_tier": "production"})})
	res.allowed
	"rds_multi_az_in_production" in rules_warned(res)
}

test_public_read_bucket_is_denied {
	res := guardrails.result with input as mk({"B": {"Type": "AWS::S3::Bucket", "Properties": {"AccessControl": "PublicRead"}}}, {})
	"s3_not_public" in rules_denied(res)
}

test_bucket_without_public_access_block_warns_but_is_allowed {
	res := guardrails.result with input as mk({"B": {"Type": "AWS::S3::Bucket", "Properties": {}}}, {})
	res.allowed
	"s3_not_public" in rules_warned(res)
}

test_fully_blocked_bucket_is_clean {
	b := {"Type": "AWS::S3::Bucket", "Properties": {"PublicAccessBlockConfiguration": {
		"BlockPublicAcls": true, "BlockPublicPolicy": true, "IgnorePublicAcls": true, "RestrictPublicBuckets": true}}}
	res := guardrails.result with input as mk({"B": b}, {})
	count(res.deny) == 0
	count(res.warn) == 0
}

test_open_ssh_is_denied {
	sg := {"Type": "AWS::EC2::SecurityGroup", "Properties": {"SecurityGroupIngress": [
		{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "0.0.0.0/0"}]}}
	res := guardrails.result with input as mk({"Sg": sg}, {})
	"sg_no_open_admin_ports" in rules_denied(res)
}

test_open_http_and_https_are_allowed {
	sg := {"Type": "AWS::EC2::SecurityGroup", "Properties": {"SecurityGroupIngress": [
		{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "CidrIp": "0.0.0.0/0"},
		{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIpv6": "::/0"}]}}
	res := guardrails.result with input as mk({"Sg": sg}, {})
	res.allowed
}

test_open_all_traffic_is_denied {
	sg := {"Type": "AWS::EC2::SecurityGroup", "Properties": {"SecurityGroupIngress": [
		{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}]}}
	res := guardrails.result with input as mk({"Sg": sg}, {})
	"sg_no_open_admin_ports" in rules_denied(res)
}

test_a_range_containing_web_ports_but_wider_is_denied {
	sg := {"Type": "AWS::EC2::SecurityGroup", "Properties": {"SecurityGroupIngress": [
		{"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535, "CidrIp": "0.0.0.0/0"}]}}
	res := guardrails.result with input as mk({"Sg": sg}, {})
	"sg_no_open_admin_ports" in rules_denied(res)
}

test_private_cidr_ingress_is_fine {
	sg := {"Type": "AWS::EC2::SecurityGroup", "Properties": {"SecurityGroupIngress": [
		{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "CidrIp": "10.0.0.0/16"}]}}
	res := guardrails.result with input as mk({"Sg": sg}, {})
	res.allowed
}

test_internet_facing_lb_for_private_service_is_denied {
	lb := {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {"Scheme": "internet-facing"}}
	res := guardrails.result with input as mk({"Lb": lb}, {"intent_spec": object.union(spec, {"public_facing": false})})
	"lb_scheme_matches_intent" in rules_denied(res)
}

test_internal_lb_for_private_service_is_fine {
	lb := {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {"Scheme": "internal"}}
	res := guardrails.result with input as mk({"Lb": lb}, {"intent_spec": object.union(spec, {"public_facing": false})})
	res.allowed
}

test_wildcard_admin_iam_is_denied {
	role := {"Type": "AWS::IAM::Role", "Properties": {"Policies": [
		{"PolicyName": "admin", "PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}}]}}
	res := guardrails.result with input as mk({"Role": role}, {})
	"iam_no_wildcard_admin" in rules_denied(res)
}

test_wildcard_action_in_a_list_is_denied {
	pol := {"Type": "AWS::IAM::ManagedPolicy", "Properties": {"PolicyDocument": {"Statement": [
		{"Effect": "Allow", "Action": ["s3:GetObject", "*"], "Resource": ["*"]}]}}}
	res := guardrails.result with input as mk({"Pol": pol}, {})
	"iam_no_wildcard_admin" in rules_denied(res)
}

test_scoped_iam_is_fine {
	role := {"Type": "AWS::IAM::Role", "Properties": {"Policies": [
		{"PolicyName": "logs", "PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "*"}]}}]}}
	res := guardrails.result with input as mk({"Role": role}, {})
	res.allowed
}

test_attached_resource_without_retain_is_denied {
	db := {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": true}}
	res := guardrails.result with input as mk({"Db": db}, {"existing_resources": {"database": {"id": "orders-db"}}})
	"imports_are_retained" in rules_denied(res)
}

test_cost_over_budget_is_denied {
	res := guardrails.result with input as mk({"Db": good_db}, {"estimated_monthly_cost_usd": 250})
	"within_budget" in rules_denied(res)
}

test_cost_within_budget_or_no_budget_is_fine {
	res := guardrails.result with input as mk({"Db": good_db}, {"estimated_monthly_cost_usd": 100})
	res.allowed
	nb := guardrails.result with input as mk({"Db": good_db}, {"intent_spec": {"monthly_budget_usd": null}, "estimated_monthly_cost_usd": 9999})
	nb.allowed
}

test_checks_only_list_applicable_rules {
	res := guardrails.result with input as mk({"Db": good_db}, {})
	names := {c.check | some c in res.checks}
	"rds_encryption_at_rest" in names
	not "s3_not_public" in names
	"within_budget" in names
}

test_check_status_reflects_findings {
	res := guardrails.result with input as mk({"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": false, "PubliclyAccessible": false}}}, {})
	by_name := {c.check: c.status | some c in res.checks}
	by_name.rds_encryption_at_rest == "fail"
	by_name.rds_not_public == "pass"
}

test_an_empty_template_is_allowed_with_only_global_checks {
	res := guardrails.result with input as mk({}, {})
	res.allowed
	count(res.checks) == 2
}
