# Smart AI DevOps Platform — System Architecture Visual Guide

> **Core Philosophy**: Most CI/CD systems deploy blindly and wait for human alerts. This platform functions like an **automated flight control tower**: it deploys a canary alongside production, uses genuine statistical tests to evaluate live behavior at incremental steps (10% → 25% → 50% → 100%), cryptographically signs every verdict, gates every action through OPA policy, and rolls back automatically before human customers ever notice an issue.

---

## 1. Executive Summary & Plain-English Analogy

```
   Traditional Deployment (Blind Drop)                 This Platform (Self-Verifying Canary)
   ───────────────────────────────────                 ─────────────────────────────────────
   Deploy 100% ──▶ Hope it doesn't crash               Deploy 10% ──▶ Test Statistics ──▶ Pass?
         │                                                    │                              │
         ▼                                                    ▼                              ▼
   Users complain ──▶ 3 AM PagerDuty call              Rollback in ms              Promote to 25%... 100%
```

Imagine an **Automated High-Speed Railway Switch**:
1. **The Code Train Arrives (GitHub)**: A developer pushes code.
2. **The Pre-Boarding Inspection (Gate 1)**: The system dry-runs and builds the code in an isolated sandbox. If it fails, an AI engineer explains why in plain English.
3. **The Test Track (Canary Deployment)**: 10% of real passenger traffic is routed to the new train, while 90% stays on the proven baseline.
4. **The Science Lab (Verification Engine)**: Sensors measure passenger satisfaction, braking distance, and engine heat. Instead of asking a simple question like *"is temperature > 80°?"*, mathematical algorithms compare the canary against the baseline distribution.
5. **The Safety Inspector (Policy Controller + OPA)**: Verifies the lab's official wax seal (HMAC signature) and checks company safety bylaws (freeze windows, budget limits).
6. **The Switch Actuation**: If healthy, the track gradually shifts to 25%, 50%, and finally 100%. If any anomaly is detected, traffic switches back to 0% in milliseconds with zero downtime.

---

## 2. High-Level Visual Flow (Non-Technical View)

```mermaid
flowchart TD
    classDef client fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0369A1;
    classDef gateway fill:#F3E8FF,stroke:#9333EA,stroke-width:2px,color:#6B21A8;
    classDef worker fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#92400E;
    classDef stats fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#166534;
    classDef policy fill:#FEE2E2,stroke:#DC2626,stroke-width:2px,color:#991B1B;
    classDef cloud fill:#F1F5F9,stroke:#475569,stroke-width:2px,color:#334155;

    Dev["👨‍💻 Developer Git Push"] -->|"1. Triggers Webhook"| GW["🌐 API Gateway\n(Security & Authentication)"]:::gateway
    GW -->|"2. Dispatches Build Job"| Worker["⚙️ Pipeline Worker\n(Builds & Deploys Canary)"]:::worker
    
    subgraph DeployPhase ["Traffic Split (Zero Pod Restarts)"]
        Worker -->|"3. Routes 10% Traffic"| Targets["🎯 Live Environment\n(AWS ECS / Kubernetes)"]:::cloud
    end

    Targets -->|"4. Telemetry Stream\n(Latency, Errors, CPU)"| Engine["🔬 Verification Engine\n(Statistical Math Lab)"]:::stats
    Engine -->|"5. HMAC-Signed Verdict\n(HEALTHY or FAILED)"| Controller["🛡️ Policy Controller + OPA\n(Rules & Safety Gate)"]:::policy

    Controller -->|"6a. PASS: Shift Traffic Up (25% → 100%)"| Targets
    Controller -->|"6b. FAIL: Instant Rollback (0% Canary)"| Targets
    Controller -->|"7. Plain-English Incident Report"| AI["🤖 Explainability AI (Groq)\n(Grounded Root Cause Analysis)"]:::client
    AI -->|"8. Live Updates"| UI["🖥️ Modern Web Console\n(React SPA)"]:::client
```

---

## 3. End-to-End System Architecture (Detailed Technical Map)

This diagram preserves **100% of the nodes, queues, databases, and microservices** from `System_architecture.pdf` while organizing them into clean functional zones:

```mermaid
flowchart TB
    %% STYLING
    classDef clientZone fill:#F0F9FF,stroke:#0284C7,stroke-width:2px,color:#075985;
    classDef apiZone fill:#FAF5FF,stroke:#9333EA,stroke-width:2px,color:#581C87;
    classDef redisZone fill:#FFFBEB,stroke:#F59E0B,stroke-width:2px,color:#78350F;
    classDef coreZone fill:#F0FDF4,stroke:#16A34A,stroke-width:2px,color:#14532D;
    classDef dataZone fill:#EFF6FF,stroke:#3B82F6,stroke-width:2px,color:#1E3A8A;
    classDef deployZone fill:#F8FAFC,stroke:#64748B,stroke-width:2px,color:#0F172A;
    classDef aiZone fill:#FEF2F2,stroke:#EF4444,stroke-width:2px,color:#7F1D1D;

    %% 1. CLIENT LAYER
    subgraph ClientLayer ["1. Client & Integration Layer"]
        UI["🖥️ React SPA (Vite + TypeScript)\n• Project Workspaces\n• Live Graphs & Audit Logs"]:::clientZone
        GitHub["🐙 GitHub Enterprise / Cloud\n• OAuth 2.0 Auth Code Flow\n• HMAC-Signed Push Webhooks"]:::clientZone
    end

    %% 2. EDGE API GATEWAY
    subgraph ApiLayer ["2. Edge & Security Layer — api-gateway (FastAPI)"]
        AuthModule["🔐 Auth & Session\n• bcrypt + HS256 JWT\n• Redis Revocable Refresh Tokens"]:::apiZone
        TenantMiddleware["🏢 Tenant Middleware\n• SET LOCAL app.active_tenant_id\n• True Postgres Row-Level Security"]:::apiZone
        RestRouters["📡 REST Routers\n• /projects • /pipelines\n• /policy • /reports • /audit"]:::apiZone
        StreamGateways["⚡ Real-Time Streaming\n• WebSocket /ws/pipelines (events)\n• SSE over fetch() (raw logs)"]:::apiZone
    end

    %% 3. ASYNC BACKBONE
    subgraph AsyncBackbone ["3. Async Backbone — Redis 7.0"]
        StreamStart["📥 Stream: pipeline:start\n(Consumer Group Dispatch)"]:::redisZone
        StreamGate1["🧪 Stream: gate1:check\n(Dry-Run Preflight)"]:::redisZone
        StreamVerdicts["📜 Stream: verdicts\n(HMAC-SHA256 Signed Verdicts)"]:::redisZone
        RedisState["🔒 Cache & Distributed Locks\n• rollout_state:{run_id}\n• lock:pipeline:{tenant}:{svc}\n• actuation_target:{run_id}"]:::redisZone
    end

    %% 4. CORE ENGINE SERVICES
    subgraph CoreServices ["4. Core Engine Microservices"]
        Worker["⚙️ pipeline-worker (Python 3.12)\n• DAG: build → test → deploy → canary_loop\n• reconciler.py (crash recovery resume)\n• Gate 1 Dry-Run Execution"]:::coreZone
        
        Engine["🔬 verification-engine (Python 3.11)\n• Wald SPRT (Error Rate)\n• Mann-Whitney U & KS (Latency)\n• CUSUM & BOCPD (Saturation)\n• Fisher's Exact & Chi² (Business KPIs)\n• Isolation Forest (Cross-Metric Anomaly)\n⛔ Invariant: No k8s/AWS Access"]:::coreZone
        
        Controller["🛡️ policy-controller (Python 3.12)\n• Cryptographic Signature Verification\n• Freshness Window Check\n• rollout_scheduler.py (Step Dwell & Multi-Ramp)\n• Traffic Shift Actuation / Rollback"]:::coreZone
        
        ExplainService["💡 explainability-service (Python 3.12)\n• Exact-Citation Grounded Evidence\n• ChatOps Natural Language Engine\n• Groq Fast Inference (Narrates Only)"]:::coreZone
    end

    %% 5. POLICY & DATA
    subgraph SafetyAndData ["5. Governance & Data Layer"]
        OPA["⚖️ Open Policy Agent (OPA 0.68)\n• policies/delivery_guardrails.rego\n• Friday/Holiday Freeze Windows\n• Minimum Sample Size (N ≥ 100)\n• Cost-Delta Ceilings & Admin Approvals"]:::dataZone
        
        Postgres["🗄️ PostgreSQL 16 (Multi-Tenant RLS)\n• projects • pipeline_executions\n• execution_state • verification_records\n• audit_ledger • cost_analysis"]:::dataZone
    end

    %% 6. DEPLOY TARGETS
    subgraph DeployTargets ["6. Dual Cloud Deploy Targets (Routing-Layer Shifts)"]
        subgraph TargetK8s ["Kubernetes Target (Kind / EKS)"]
            Envoy["🔀 Envoy Gateway\nHTTPRoute Weights (10/90 → 100/0)"]:::deployZone
            K8sPods["📦 Baseline & Canary Deployments\n(Zero pod restarts during shift)"]:::deployZone
            Prometheus["📊 Prometheus\n(Scrapes live container metrics)"]:::deployZone
        end

        subgraph TargetAWS ["AWS ECS Target (Fargate)"]
            ALB["🔀 Shared Application Load Balancer\nWeighted Target Groups (baseline/canary)"]:::deployZone
            ECSServices["☁️ ECS Fargate Tasks & ECR\n(Cohort-aware container images)"]:::deployZone
            CloudWatch["📈 AWS CloudWatch\n(Real ALB & Task metrics)"]:::deployZone
        end
    end

    %% 7. EXTERNAL AI & OPS
    subgraph ExternalObs ["7. External AI & Observability"]
        Groq["🧠 Groq Cloud API\nopenai/gpt-oss-120b\n(Strictly Advisory / Explanatory)"]:::aiZone
        LokiStack["📋 Loki + Promtail + Grafana 11.2\n(Centralized Log Aggregation & Dashboards)"]:::aiZone
    end

    %% WIRING & RELATIONSHIPS
    UI <-->|"REST, SSE & WS"| ApiLayer
    GitHub -->|"Push Webhook (HMAC)"| RestRouters
    GitHub <-->|"OAuth Token"| AuthModule

    ApiLayer -->|"XADD"| StreamStart
    ApiLayer -->|"XADD"| StreamGate1
    ApiLayer <-->|"RLS Connection"| Postgres
    ApiLayer <-->|"Session Keys"| RedisState

    StreamStart -->|"Read Group"| Worker
    StreamGate1 -->|"Dry-Run Check"| Worker
    Worker -->|"Build & Push Image"| ECSServices
    Worker -->|"Deploy Canary Pods"| K8sPods
    Worker -->|"Update State & Logs"| Postgres
    Worker -->|"POST /verify"| Engine

    Prometheus -.->|"Scraped by"| Engine
    CloudWatch -.->|"Scraped by"| Engine

    Engine -->|"Sign HMAC & XADD"| StreamVerdicts
    StreamVerdicts -->|"Read Group"| Controller
    Controller -->|"Verify Sig & Ask OPA"| OPA
    Controller -->|"Allow Action?"| Postgres

    Controller -->|"Patch HTTPRoute"| Envoy
    Controller -->|"Modify Weights"| ALB
    Controller -->|"Trigger RCA on Failure"| ExplainService
    
    ExplainService -->|"Prompt with Citations"| Groq
    ExplainService -->|"Read Context"| Postgres
    
    DeployTargets -.->|"Logs"| LokiStack
```

---

## 4. The 5 Core Invariants: Bridge from Tech to Business Value

| # | Technical Invariant | Plain-English Meaning | Business Value / Disaster Prevented |
|---|---|---|---|
| **1** | **No Static Thresholds**<br>(Wald SPRT, Mann-Whitney U, CUSUM/BOCPD) | Never check `if error > 1%`. Instead, compare canary probability curves to baseline curves. | **Prevents False Positives & False Negatives**: A 2% error rate at 3 AM might be random jitter, but at peak hours it is a disaster. Math handles the difference automatically. |
| **2** | **Routing-Layer Traffic Shifting**<br>(Gateway API HTTPRoute / ALB Listener Rules) | We move the road signs (traffic weight), we never stop and restart the cars (pods/tasks). | **Zero Downtime & Instant Rollbacks**: Pod restarts cause cold-start latency spikes. Routing shifts occur in ~20ms with no service interruption. |
| **3** | **Cryptographic Verdict Signatures**<br>(HMAC-SHA256 + Freshness Window) | The math engine stamps verdicts with a secret tamper-proof wax seal. The controller rejects unsigned verdicts. | **Zero Rogue Deployments**: Prevents compromised workers, rogue scripts, or network replay attacks from forcing an untested deployment live. |
| **4** | **Multi-Tenancy Postgres RLS**<br>(`SET LOCAL app.active_tenant_id`) | Database rows are physically invisible across tenants at the SQL engine level. | **Complete Data Privacy**: A bug in the application code can never leak customer A’s audit logs or deployment configurations to customer B. |
| **5** | **Strict Statistical Floor**<br>($N \ge 100$ enforced in Python & OPA) | No verdict can promote a release without at least 100 real requests observed. | **Prevents Premature Promotion**: Prevents rolling out broken code to 100% of users based on 2 lucky test requests. |

---

## 5. Life of a Code Change (Chronological Sequence)

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant GH as GitHub
    participant GW as API Gateway
    participant Redis as Redis Streams
    participant Worker as Pipeline Worker
    participant Cluster as Deploy Target (K8s/ECS)
    participant VE as Verification Engine
    participant PC as Policy Controller
    participant OPA as Open Policy Agent
    participant AI as Explainability (Groq)

    Dev->>GH: git push origin main
    GH->>GW: POST /api/v1/webhooks/github (HMAC signed)
    GW->>Redis: XADD stream:gate1:check
    Redis->>Worker: Consume Gate 1 (Dry run build & test)
    
    alt Gate 1 Tests Fail
        Worker->>AI: Request failure analysis
        AI-->>GW: Plain-English RCA summary
        GW-->>Dev: GitHub Commit Check: FAILED with explanation
    else Gate 1 Tests Pass
        Worker->>GW: Callback: Gate 1 Passed
        GW->>Redis: XADD stream:pipeline:start
        Redis->>Worker: Consume Start Rollout
        Worker->>Cluster: Deploy Canary Cohort & set weight to 10%
        
        loop Progressive Multi-Step Ramp (10% → 25% → 50% → 100%)
            Note over Cluster: Live user traffic hits Baseline (90%) & Canary (10%)
            Worker->>VE: POST /verify (run_id, step)
            VE->>Cluster: Collect telemetry (Prometheus / CloudWatch)
            VE->>VE: Execute Wald SPRT, Mann-Whitney U, CUSUM
            VE->>Redis: XADD stream:verdicts (Signed ImmutableVerdict)
            Redis->>PC: Consume Verdict
            PC->>PC: Verify HMAC-SHA256 signature & freshness
            PC->>OPA: Query delivery_guardrails.rego (Freeze window? N >= 100?)
            
            alt OPA says ALLOW & Verdict is HEALTHY
                OPA-->>PC: allow_action = true
                PC->>Cluster: Shift traffic to next step (e.g. 25%)
            else OPA says DENY or Verdict is FAILED
                OPA-->>PC: allow_action = false (or rollback triggered)
                PC->>Cluster: Emergency Rollback: Shift traffic to 0%, scale canary to 0
                PC->>AI: Request Grounded Root Cause Analysis
                AI-->>Dev: Incident alert with statistical proof
            end
        end
    end
```

---

## 6. How the Statistical Algorithms Work (Non-Math Summary)

```
┌─────────────────────────┬───────────────────────────────────┬──────────────────────────────────────────┐
│ Verification Test       │ What It Looks At                  │ Everyday Intuition                       │
├─────────────────────────┼───────────────────────────────────┼──────────────────────────────────────────┤
│ Wald SPRT               │ Failed vs. Successful requests    │ Like flipping a coin repeatedly to       │
│ (Sequential Ratio Test) │ over time                         │ decide whether it's rigged without       │
│                         │                                   │ needing 10,000 flips first.              │
├─────────────────────────┼───────────────────────────────────┼──────────────────────────────────────────┤
│ Mann-Whitney U          │ Full response time curve          │ Compares the full distribution of        │
│                         │ (p50, p95, p99)                   │ speeds, so a few slow outliers don't     │
│                         │                                   │ fool the system into a false alarm.      │
├─────────────────────────┼───────────────────────────────────┼──────────────────────────────────────────┤
│ CUSUM + BOCPD           │ Memory & CPU consumption          │ Detects subtle gradual leaks (like a     │
│ (Changepoint Detection) │ trends                            │ dripping pipe) before the water tank     │
│                         │                                   │ completely overflows.                    │
├─────────────────────────┼───────────────────────────────────┼──────────────────────────────────────────┤
│ Fisher's Exact / Chi²   │ Real business conversions         │ Guarantees that users are still able to  │
│                         │ (e.g. checkout click %)           │ complete purchases at the same rate      │
│                         │                                   │ as before the deployment.                │
├─────────────────────────┼───────────────────────────────────┼──────────────────────────────────────────┤
│ Isolation Forest        │ Cross-metric relationship         │ An AI scout that detects weird combinations│
│                         │ (e.g. CPU low but latency high)   │ that look normal in isolation.           │
└─────────────────────────┴───────────────────────────────────┴──────────────────────────────────────────┘
```

---

## 7. Dual Deployment Targets: Kubernetes vs. AWS ECS

The platform abstracts deployment so the same verification and policy engine controls both:

1. **Local & High-Control (Kubernetes / Kind)**:
   - Uses Gateway API `HTTPRoute` rules.
   - Traffic splits are handled by **Envoy Gateway**.
   - Telemetry is scraped in sub-seconds via **Prometheus**.
2. **Production Cloud (AWS ECS Fargate)**:
   - Uses a **single shared Application Load Balancer (ALB)** across all projects to minimize AWS cost.
   - Traffic splits are executed by adjusting listener rule target group weights.
   - Telemetry is queried via **AWS CloudWatch**.
   - Builds push directly to **Amazon ECR**.

---

## 8. Summary of Architectural Achievements

- **True Zero-Downtime Autonomous Rollouts**: Real multi-step progression (10% → 25% → 50% → 100%) with automatic pause for platform-admin manual approval at 100% cutover.
- **Fail-Soft Self-Healing**: If any worker crashes, `reconciler.py` automatically picks up the interrupted stage from PostgreSQL upon reboot without duplicate executions.
- **Explainable AI with Grounded Guardrails**: LLMs are never allowed to make deployment decisions; they only narrate and analyze mathematical evidence after the fact.
