import { Link } from "react-router-dom";
import { ArrowRight, GitBranch, ShieldCheck, Sparkles, Activity, Lock, ScrollText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

const STEPS = [
  {
    icon: GitBranch,
    title: "Ship a new version",
    body: "It deploys as a small canary running alongside your currently-live baseline — never replacing it outright.",
  },
  {
    icon: Activity,
    title: "Real traffic, both cohorts",
    body: "A slice of live traffic hits the canary while the rest keeps hitting baseline, so the comparison is apples-to-apples.",
  },
  {
    icon: Sparkles,
    title: "Statistics decide, not a dashboard-watcher",
    body: "SPRT, Mann-Whitney, CUSUM, Fisher's exact — real hypothesis tests compare error rate, latency, saturation, and a business metric.",
  },
  {
    icon: ShieldCheck,
    title: "Acts within your policy",
    body: "Healthy and confident enough → promotes automatically. A critical regression → rolls back automatically. Anything else → holds for a human.",
  },
];

const FOUNDATIONS = [
  {
    icon: Sparkles,
    title: "No naive thresholds",
    body: "Every decision comes from a real statistical test — SPRT, Mann-Whitney U, CUSUM/BOCPD, Fisher's exact/χ² — never a bare \"error rate > 1%\" check.",
  },
  {
    icon: Lock,
    title: "Cryptographically signed verdicts",
    body: "Every verdict is HMAC-SHA256 signed before it reaches the policy controller, and the signature (plus a freshness window) is verified before anything is ever actuated.",
  },
  {
    icon: ScrollText,
    title: "A real audit trail",
    body: "Every promotion, rollback, and policy change is written to a queryable Postgres audit log — exportable as a SOC 2-shaped CSV, sourced from the actual events that happened.",
  },
];

export function Landing() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur-md">
        <div className="container flex h-14 items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <ShieldCheck className="h-4 w-4" />
            </div>
            <span className="text-sm font-semibold">Smart AI DevOps Platform</span>
          </div>
          <Button asChild size="sm">
            <Link to="/login">
              Sign in <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </Button>
        </div>
      </header>

      <section className="relative overflow-hidden">
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.15]"
          style={{
            backgroundImage:
              "linear-gradient(hsl(var(--foreground) / 0.4) 1px, transparent 1px), linear-gradient(90deg, hsl(var(--foreground) / 0.4) 1px, transparent 1px)",
            backgroundSize: "40px 40px",
            maskImage: "radial-gradient(ellipse 60% 60% at 50% 0%, black, transparent)",
          }}
        />
        <div className="container relative py-20 text-center">
          <div className="mx-auto mb-5 inline-flex items-center gap-1.5 rounded-full border bg-muted/40 px-3 py-1 text-code text-[11px] text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-success" />
            Autonomous, statistically-verified continuous delivery
          </div>
          <h1 className="mx-auto max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">
            Continuous delivery that verifies its own deployments with{" "}
            <span className="bg-gradient-to-r from-primary via-success to-primary bg-clip-text text-transparent">
              hard math
            </span>
            , not gut feelings.
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-muted-foreground">
            Statistical canary analysis and autonomous, policy-gated promote/rollback decisions —
            so no one has to stare at a dashboard for twenty minutes after every deploy.
          </p>
          <div className="mt-8 flex justify-center gap-3">
            <Button asChild size="lg">
              <Link to="/login">
                Get started <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          </div>
        </div>
      </section>

      <section className="container pb-16">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {STEPS.map((step, i) => (
            <Card key={step.title}>
              <CardHeader>
                <div className="mb-2 flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <step.icon className="h-5 w-5" />
                </div>
                <CardTitle>
                  {i + 1}. {step.title}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <CardDescription>{step.body}</CardDescription>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      <section className="border-t bg-muted/20 py-16">
        <div className="container">
          <span className="text-code text-xs font-semibold uppercase tracking-widest text-primary">
            Mathematical foundations
          </span>
          <h2 className="mt-1 text-2xl font-semibold tracking-tight">
            Autonomous decisions built on proof, not polls
          </h2>
          <div className="mt-6 grid gap-4 md:grid-cols-3">
            {FOUNDATIONS.map((f) => (
              <Card key={f.title}>
                <CardHeader>
                  <div className="mb-2 flex h-9 w-9 items-center justify-center rounded-md bg-success/10 text-success">
                    <f.icon className="h-5 w-5" />
                  </div>
                  <CardTitle>{f.title}</CardTitle>
                </CardHeader>
                <CardContent>
                  <CardDescription>{f.body}</CardDescription>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </section>

      <footer className="border-t py-6 text-center text-xs text-muted-foreground">
        Smart AI DevOps & Continuous Delivery Platform
      </footer>
    </div>
  );
}
