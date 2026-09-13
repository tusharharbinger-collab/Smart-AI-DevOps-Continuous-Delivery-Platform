import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { toast } from "sonner";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { login } from "@/api/auth";

export function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("demo@acme-corp.test");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      toast.success("Signed in");
      navigate("/projects");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 px-4 py-8">
      <div className="grid w-full max-w-4xl grid-cols-1 overflow-hidden rounded-2xl border bg-card shadow-xl lg:grid-cols-12">
        {/* Left brand panel */}
        <div className="flex flex-col justify-between border-b bg-gradient-to-br from-primary/10 via-transparent to-transparent p-8 lg:col-span-5 lg:border-b-0 lg:border-r">
          <div>
            <div className="mb-6 flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <ShieldCheck className="h-4.5 w-4.5" />
              </div>
              <span className="text-sm font-semibold">Smart AI DevOps Platform</span>
            </div>
            <h2 className="text-xl font-bold leading-tight tracking-tight">
              Autonomous, statistically-verified continuous delivery
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">
              Real Wald SPRT hypothesis testing, OPA policy guardrails, and a signed, queryable audit trail.
            </p>

            <div className="mt-6 space-y-1 rounded-lg border bg-background/60 p-3 text-code text-[11px] text-muted-foreground">
              <div className="flex items-center justify-between border-b pb-1.5 text-[10px]">
                <span className="font-semibold text-success">verification-engine</span>
                <span>ready</span>
              </div>
              <div className="pt-1 leading-relaxed">
                <div>&gt; evaluating hypothesis H0 vs H1</div>
                <div className="text-primary">&gt; sample size N ≥ 100 (floor enforced)</div>
                <div className="text-success">&gt; verdict signed (HMAC-SHA256)</div>
              </div>
            </div>
          </div>
        </div>

        {/* Right auth panel */}
        <div className="flex flex-col justify-center p-8 lg:col-span-7">
          <Link to="/" className="mb-4 text-xs text-muted-foreground hover:underline">
            &larr; Back
          </Link>
          <h1 className="text-lg font-semibold">Sign in</h1>
          <p className="mb-6 text-sm text-muted-foreground">Access your team's delivery workspace.</p>

          <form onSubmit={handleSubmit} className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>

            {error && <p className="text-xs text-destructive">{error}</p>}

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
              {!submitting && <ArrowRight className="h-3.5 w-3.5" />}
            </Button>

            <p className="text-[11px] text-muted-foreground">
              Demo tenant: demo@acme-corp.test / acme-demo-2026
            </p>
          </form>
        </div>
      </div>
    </div>
  );
}
