"""
shared/provisioning/aws_arn.py

IAM role ARN validation, in its own module with NO third-party imports: api-gateway (which has no boto3 in its
image) validates a customer-supplied role ARN with this before anything is sent to STS, while
aws_session.py (which needs boto3) re-exports it for the provisioning side.
"""
import re

# arn:aws:iam::123456789012:role/name-or-path/name  (also aws-cn / aws-us-gov partitions)
_ROLE_ARN_RE = re.compile(r"^arn:aws(?:-cn|-us-gov)?:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")


def parse_role_arn(role_arn: str) -> str:
    """Returns the 12-digit account id, or raises ValueError."""
    match = _ROLE_ARN_RE.match(role_arn or "")
    if not match:
        raise ValueError("role_arn must look like arn:aws:iam::<12-digit account id>:role/<role name>")
    return match.group(1)
