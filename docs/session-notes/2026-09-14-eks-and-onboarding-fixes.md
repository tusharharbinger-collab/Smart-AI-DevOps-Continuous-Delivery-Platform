# Session notes — 2026-09-14: EKS migration follow-through + real onboarding bugs

## Where things stand

- The platform runs on real AWS EKS this session (cluster `smartcd-eks`,
  `us-east-1`), not Kind — Envoy Gateway, Gateway API, and the `payments-pipeline`
  baseline/canary Deployments are live and verified against it.
- **AWS is being torn down at the end of this session to stop billing.**
  See "Tomorrow: recreating the cluster" below — this is a normal,
  expected step, not data loss. Nothing in Postgres/Redis depends on the
  cluster existing; `EKS_CLUSTER_NAME` is blanked in `.env` afterward so
  the code gracefully falls back instead of erroring on a dead cluster
  reference (see `shared/eks_auth.py` — it raises on `describe_cluster`
  failure if the name is set but stale).

## Real bugs found and fixed today

1. **Pause/Resume did no real work on a terminal run.**
   `services/api-gateway/src/routers/actuation_router.py` — `resume_pipeline`/
   `pause_pipeline` used to flip Redis's cached status with zero validation
   of the run's actual state, and never wrote to Postgres's durable
   `execution_state`. Clicking Resume on an already-`FAILED` run faked a
   "RUNNING" display forever. Fixed: both now require the correct current
   status (`PAUSED`→resume, `RUNNING`→pause) and write both stores
   together, or reject with a clear 409.

2. **`DEGRADED` verdicts got no audit entry and no RCA.**
   `services/policy-controller/src/controller.py` — the generic
   "OPA blocked this promotion" branch (covers `DEGRADED` verdicts, freeze
   windows, cost-delta breaches) never called `record_actuation` or
   `trigger_rca_async`. Fixed: it now writes a real `audit_ledger` row
   (`action='BLOCK'` — the CHECK constraint doesn't allow `'BLOCKED'`,
   watch for that) and generates a real RCA, same as rollback/promotion.

3. **Onboarding a real GitHub repo (`test-`) surfaced 3 more real, structural
   gaps in the Phase 8 project wizard — never previously exercised against
   an arbitrary non-demo repo:**
   - `container_image` validation only checked lowercase, not full Docker
     reference syntax — a repo named `test-` (trailing hyphen) produced
     `registry.internal/test-`, which is lowercase but still an invalid
     Docker reference (components can't start/end with `-`/`.`/`_`).
     Fixed in `projects_router.py` (backend) and `NewProject.tsx`'s
     `dockerSafeName()` (frontend default).
   - The `test` stage never ran inside the repo it had just cloned —
     `build_task.py`'s `run_build_task` deleted the clone in a `finally`
     block before the test stage ever ran, and `run_test_task` had no
     `cwd` parameter anyway. Fixed: the workspace path is now returned
     from the build stage, threaded through `worker.py` as `cwd` for the
     test stage, and cleaned up once at the very end of the whole pipeline
     run instead.
   - **The wizard's generated pipeline YAML never had a `deploy` stage at
     all.** `canary_loop` only shifts `HTTPRoute` traffic weight and runs
     verification — it never touches a Deployment's image. Every
     wizard-onboarded project could report `COMPLETED` while the actual
     canary pod sat on whatever placeholder image onboarding gave it,
     forever. Fixed: `generate_project_pipeline_yaml` now emits a real
     `canary_deploy` stage; `deploy_task.py` got a new
     `deploy_project_canary_task` (a generic strategic-merge-patch of the
     onboarding-created Deployment, since the existing `deploy_canary_task`
     is hardcoded to the payments-service demo's name/namespace/image).

   All three fixed and verified live against the real `test-` repo and EKS
   cluster — `test-canary` and `test-baseline` are both `1/1 Running` with
   the real pushed image.

## Not yet done — planned for next session

- A more systematic shakeout of the onboarding wizard: deliberately onboard
  2-3 more varied test repos (different languages/structures, a private
  repo, an `existing_image` source) to proactively surface remaining gaps
  in that path, rather than finding them one at a time on real usage.
- Frontend: Pause/Resume/Emergency Rollback buttons still show as enabled
  for a terminal-status run in the UI — should be disabled/hidden based on
  actual status, now that the backend correctly rejects the invalid
  transition instead of silently faking it.
- Root AWS account password rotation — flagged earlier this session after
  it was pasted in chat by mistake; still the user's own action to do,
  cannot be done from here.
- `eks-down`'s reliability: `eksctl delete cluster` alone has twice left an
  orphaned classic ELB + auto-generated `k8s-elb-*` security group behind
  (created by the Kubernetes `Service type=LoadBalancer` Envoy Gateway
  makes, which `eksctl` doesn't know about), blocking VPC/subnet deletion.
  Today's teardown deletes the ELB/security group *before* calling
  `eksctl delete cluster` to pre-empt this — worth turning into a proper
  `make eks-down` fix instead of doing it by hand each time.

## Tomorrow: recreating the cluster

Everything needed is already scripted from today's work. Rough order:

```bash
make eks-up              # eksctl, ~10-12 min — cluster + managed node group
make install-envoy-eks   # Envoy Gateway + Gateway API CRDs (~2 min)
make ecr-up               # idempotent; the `payments` and `test` ECR repos already exist
```

Then re-apply the app-specific manifests (these aren't behind one Makefile
target yet — see `k8s/gateway/*.yaml`, `k8s/eks/payments-service/*.yaml`):

```bash
kubectl create namespace production
kubectl apply -f k8s/gateway/gateway-class.yaml
kubectl apply -f k8s/gateway/httproute-payments.yaml
kubectl apply -f k8s/eks/payments-service/baseline-deployment.yaml
kubectl apply -f k8s/eks/payments-service/canary-deployment.yaml
```

The `test` project's namespace/Deployments/HTTPRoute (`tenant-aaaaaaaa`,
`test-baseline`/`test-canary`/`test-route`) were provisioned automatically
by the onboarding wizard when the project was created — they do NOT need
manual re-`kubectl apply`, but they WILL need their images re-pushed to
ECR and a fresh rollout triggered once the cluster exists again (ECR
repositories persist across cluster teardown; the images inside them do
too, so `test:v1.1.0` should still be pullable — worth checking before
assuming a rebuild is needed).

Finally, set `EKS_CLUSTER_NAME=smartcd-eks` back in `.env` and recreate
`pipeline-worker`/`policy-controller` (`docker compose up -d --force-recreate
pipeline-worker policy-controller`) — docker-compose only re-reads `.env`
at container creation, a plain restart won't pick it up. Verify with
`curl localhost:8001/readyz` — should show `"kubernetes":"ok"`.
