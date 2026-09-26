"""
services/explainability-service/src/log_hygiene_analyzer.py

AI Log & Code Hygiene Analyzer.
Scans source code files / commit diffs and recent CloudWatch / container logs to:
1. Detect noisy console.log(), console.debug(), and Python print() statements.
2. Flag potential sensitive data leaks (tokens, passwords, PII printed to stdout).
3. Estimate CloudWatch ingestion cost waste ($0.50/GB).
4. Generate a clean, unified git diff (--- a/... +++ b/...) to sanitize/remove them.
5. Provide prescriptive architectural recommendations (e.g. structured logging, ESLint rules).

Runs with a Groq LLM pass for intelligent refactoring, with a deterministic regex/AST fallback.
"""
import json
import os
import re
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# AWS CloudWatch Logs ingestion pricing in us-east-1 ($0.50 per GB)
CLOUDWATCH_INGESTION_PER_GB_USD = 0.50

_LOG_HYGIENE_SYSTEM_PROMPT = (
    "You are an expert Cloud DevOps & Code Quality SRE. "
    "Your mission is to audit container application logs and source code to eliminate noisy, "
    "expensive, and dangerous console logs (e.g. console.log, console.debug, Python print statements, unformatted stdout dumps). "
    "Analyze the provided code and runtime CloudWatch log samples. "
    "Identify exact lines where noisy or insecure logging occurs. "
    "Generate a clean, valid unified git diff (--- a/... +++ b/...) that either safely removes "
    "redundant debug console logs or upgrades them to structured, sanitized logging without breaking business logic. "
    "Respond ONLY with a valid JSON object matching this schema: "
    "summary (string, brief executive summary of hygiene findings), "
    "detected_issues (array of {file_path: string, line_number: integer or null, statement: string, issue_type: 'DEBUG_NOISE' | 'SENSITIVE_LEAK_RISK' | 'HIGH_VOLUME_SPAM', reason: string}), "
    "suggested_patch (string or null, valid unified diff: --- a/... +++ b/...), "
    "estimated_monthly_savings_usd (number, estimated CloudWatch savings), "
    "recommended_best_practices (array of strings, e.g. ESLint no-console rule, babel-plugin-transform-remove-console, pino/winston)."
)


class DetectedLogIssue(BaseModel):
    file_path: str
    line_number: int | None = None
    statement: str
    issue_type: str = "DEBUG_NOISE"
    reason: str


class LogHygieneReport(BaseModel):
    summary: str
    detected_issues: list[DetectedLogIssue] = Field(default_factory=list)
    suggested_patch: str | None = None
    estimated_monthly_savings_usd: float = 0.0
    recommended_best_practices: list[str] = Field(default_factory=list)


def _scan_code_for_console_logs(code_files: dict[str, str]) -> list[DetectedLogIssue]:
    """
    Deterministic regex scanner for JavaScript/TypeScript console.* and Python print() statements.
    """
    issues: list[DetectedLogIssue] = []
    
    # Patterns for console.log/debug/info/warn/dir/trace and print()
    js_console_re = re.compile(r"^\s*console\.(log|debug|info|dir|trace)\s*\((.*)\);?\s*$", re.MULTILINE)
    py_print_re = re.compile(r"^\s*print\s*\((.*)\)\s*$", re.MULTILINE)
    sensitive_keyword_re = re.compile(r"(token|secret|password|key|auth|cred|bearer|jwt)", re.IGNORECASE)

    for file_path, content in code_files.items():
        if not content:
            continue
        
        is_js = file_path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))
        is_py = file_path.endswith(".py")
        
        lines = content.splitlines()
        for idx, line in enumerate(lines, start=1):
            match = None
            if is_js:
                match = js_console_re.search(line)
            elif is_py:
                match = py_print_re.search(line)

            if match:
                matched_stmt = line.strip()
                
                # Check for sensitive leaks
                if sensitive_keyword_re.search(line):
                    issue_type = "SENSITIVE_LEAK_RISK"
                    reason = "Logs sensitive security keyword (token/password/key); potential credential leak to CloudWatch"
                elif "loop" in line.lower() or (idx > 50 and "i++" in line):
                    issue_type = "HIGH_VOLUME_SPAM"
                    reason = "Potential high-frequency log inside tight loop causing CloudWatch bill spikes"
                else:
                    issue_type = "DEBUG_NOISE"
                    reason = "Ad-hoc console log left in production code; pollutes telemetry"

                issues.append(
                    DetectedLogIssue(
                        file_path=file_path,
                        line_number=idx,
                        statement=matched_stmt,
                        issue_type=issue_type,
                        reason=reason,
                    )
                )

    return issues


def _generate_deterministic_patch(code_files: dict[str, str], issues: list[DetectedLogIssue]) -> str | None:
    """
    Generates a deterministic unified diff removing identified console logs.
    """
    if not issues:
        return None

    diff_chunks = []
    # Group issues by file_path
    by_file: dict[str, list[DetectedLogIssue]] = {}
    for issue in issues:
        by_file.setdefault(issue.file_path, []).append(issue)

    for file_path, file_issues in by_file.items():
        original_content = code_files.get(file_path)
        if not original_content:
            continue

        original_lines = original_content.splitlines()
        
        diff_chunks.append(f"--- a/{file_path}\n+++ b/{file_path}")
        for issue in file_issues:
            if issue.line_number and 1 <= issue.line_number <= len(original_lines):
                line_idx = issue.line_number - 1
                diff_chunks.append(f"@@ -{issue.line_number},1 +{issue.line_number},0 @@")
                diff_chunks.append(f"-{original_lines[line_idx]}")

    return "\n".join(diff_chunks) if diff_chunks else None


def _fallback_log_hygiene_report(
    code_files: dict[str, str],
    cloudwatch_logs: list[str],
) -> dict[str, Any]:
    """
    Deterministic fallback when Groq is unreachable or GROQ_API_KEY is not configured.
    """
    issues = _scan_code_for_console_logs(code_files)
    
    # Calculate estimated monthly waste: each issue assumed ~500 bytes/min at 1 req/sec
    estimated_monthly_gb = len(issues) * 0.4
    estimated_savings = round(estimated_monthly_gb * CLOUDWATCH_INGESTION_PER_GB_USD, 2)
    
    patch = _generate_deterministic_patch(code_files, issues)
    
    return {
        "summary": (
            f"Detected {len(issues)} ad-hoc console/print statement(s) across {len(code_files)} file(s). "
            f"Cleaning these up will save an estimated ${estimated_savings}/month in AWS CloudWatch ingestion."
        ),
        "detected_issues": [issue.model_dump() for issue in issues],
        "suggested_patch": patch,
        "estimated_monthly_savings_usd": estimated_savings,
        "recommended_best_practices": [
            "Enable ESLint 'no-console' rule in .eslintrc.json to prevent console statements at commit time.",
            "Use 'babel-plugin-transform-remove-console' in production build bundles.",
            "Adopt structured JSON logging (Winston, Pino, or structlog) for essential telemetry.",
            "Ensure sensitive tokens and request bodies are masked before logging."
        ],
    }


async def analyze_code_and_log_hygiene(
    code_files: dict[str, str],
    cloudwatch_logs: list[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """
    Analyzes code files and CloudWatch logs for noisy console statements and generates refactoring advice.
    """
    cw_logs = cloudwatch_logs or []
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("log_hygiene_analysis_using_deterministic_fallback_no_api_key")
        return _fallback_log_hygiene_report(code_files, cw_logs)

    analysis_input = {
        "files_to_audit": {path: content[:4000] for path, content in code_files.items()},
        "recent_cloudwatch_logs_sample": cw_logs[:25],
    }

    prompt = (
        f"Audit this codebase and runtime CloudWatch log sample for noisy/unnecessary console logs:\n"
        f"{json.dumps(analysis_input, indent=2)}"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": _LOG_HYGIENE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = LogHygieneReport.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning("log_hygiene_groq_timeout", timeout_seconds=timeout_seconds)
        return _fallback_log_hygiene_report(code_files, cw_logs)
    except Exception as exc:
        logger.error("log_hygiene_groq_failed", error=str(exc))
        return _fallback_log_hygiene_report(code_files, cw_logs)
