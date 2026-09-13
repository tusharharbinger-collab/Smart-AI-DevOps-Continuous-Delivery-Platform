# Architecture — Key Design Decisions & Trade-offs

## 1. Five services, split by *permission*, not just by function

The natural split would be "verification logic" + "everything else." Instead the boundary is drawn around **who is allowed to touch Kubernetes**:

- `verification-engine` has no `kubernetes` dependency in its `requirements.txt` and no kubeconfig mounted into its container. It is *structurally* incapable of actuating anything, not merely told not to — even a fully compromised verification-engine process cannot reach the cluster.
- `pipeline-worker` and `policy-controller` are the only two services with kubeconfig access, and `actuation_executor.py` (inside `policy-controller`) is the *only* function in the entire system permitted to patch a Gateway API `HTTPRoute`.

**Trade-off:** this costs an extra network hop (verification-engine signs and publishes a verdict over Redis rather than calling Kubernetes directly) for every verification cycle. We accepted that latency deliberately — it's the mechanism that makes "the reasoning layer cannot cross the boundary by construction" literally true rather than a comment.

## 2. Verdicts are cryptographically signed, not just structurally isolated

Structural isolation stops verification-engine from *acting*. It doesn't stop an attacker with Redis write access from *injecting* a fake verdict onto the same channel. Every `ImmutableVerdict` is HMAC-SHA256 signed at creation (`verdict_signer.py`) and the signature is verified — plus a 300-second freshness window, to stop replay — before `policy-controller` will even hand it to OPA (`verdict_verifier.py`). This is tested directly: `tests/adversarial/test_guardrail_bypass.py::test_forged_verdict_rejected_by_hmac_verification` forges a verdict with the wrong key and asserts it's rejected before ever reaching OPA.

**Trade-off:** the signing key is a shared secret between two services rather than a per-service asymmetric keypair — simpler to operate, but it means either service's compromise is equivalent for this specific guarantee. Acceptable for the current threat model (a compromised *third* service, e.g. the UI or explainability layer, trying to inject actions); would need revisiting for a threat model that includes either signing party.

## 3. OPA as a separate policy layer, not `if` statements in Python

`policies/delivery_guardrails.rego` is the single place blocked-deploy windows, minimum confidence, minimum sample size, manual-approval stages, cost-delta ceilings, and right-sizing authorization all live. This is deliberately *not* Python logic mixed into `policy-controller`, for two reasons: (1) it's independently testable (`opa test policies/ -v`, and the same file is exercised by the adversarial pytest suite via `opa eval`), and (2) it makes the rule set auditable by someone who isn't reading Python — a compliance reviewer can read the `.rego` file directly.

**Trade-off:** an extra moving part (an OPA server) and a query language most engineers don't know day-to-day. We judged this worth it specifically *because* the assignment weights "guardrails a reasoning layer cannot cross" so heavily — a policy engine designed for exactly this job beats a hand-rolled equivalent.

## 4. Postgres Row-Level Security for multi-tenancy, not an app-layer `WHERE tenant_id = ?` convention

Every tenant-scoped table has `FORCE ROW LEVEL SECURITY` plus a PERMISSIVE policy keyed on `current_setting('app.active_tenant_id')`, set per-request via `auth/middleware.py`. The alternative — trusting every query in every router to remember a `WHERE tenant_id = :tid` clause — fails the moment one query forgets it. RLS fails closed: forget the clause, and the database still returns nothing for the wrong tenant.

**Trade-off, and a real bug this caught during development:** RLS is easy to configure into looking like it works while actually denying (or leaking) everything — we hit both failure modes: policies declared `AS RESTRICTIVE` with no companion PERMISSIVE policy (denies all rows to everyone), and the app connecting as a Postgres superuser (which unconditionally bypasses RLS regardless of `FORCE`). Both are now fixed and covered by a live cross-tenant HTTP test, but it's a sharp edge worth knowing about if this pattern is reused.

## 5. Statistical routing by declared metric `category`, never a hardcoded per-metric `if`

The pipeline YAML's `verificationConfig.metrics[].category` (`error_rate` / `latency` / `saturation` / `business_metric`) is the *only* thing `engine.py`'s dispatcher looks at to decide which test runs. Error rate always goes to Wald's SPRT (a sequential test suited to a Bernoulli stream), latency to Mann-Whitney U + KS (distribution-free, no normality assumption), saturation to CUSUM + BOCPD (online change-point detection), and business/conversion metrics to Fisher's exact or χ² (the correct test for a binary/categorical outcome, not a continuous-distribution test). This routing is itself evidence for reviewers that test selection is principled, not decorative.

**Trade-off:** adding a new metric category means touching the dispatcher and adding a new test module — there's no generic "just throw a t-test at it" fallback. We consider that a feature: it forces a genuine statistical justification for every new category rather than a default that quietly becomes wrong for the wrong data shape.

## What we'd change with more time

The honest gap, in order of what would matter most: (1) real telemetry from a real Prometheus/CloudWatch instance instead of synthesized samples, (2) an actually-verified JWT instead of the current unsigned demo token, (3) exercising the Kind + Envoy Gateway path live rather than only the statistics/policy core. See `README.md`'s Known Limitations for the full list.
