/**
 * frontend/src/layouts/ProjectsLayout.tsx — Phase 8.
 *
 * The shell for every /projects route: brand + workspace pill, a link back
 * to the classic pipeline console, and the session menu. Deliberately
 * slimmer than AppLayout — the pipeline/run pickers that layout carries are
 * meaningless here, because a project workspace resolves its own pipeline
 * and run from the project itself.
 */
import { Link, Navigate, Outlet, useNavigate } from "react-router-dom";
import { ChevronDown, LogOut, Plus, Settings, ShieldCheck, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import { HowItWorksDialog } from "@/components/how-it-works-dialog";
import { DevOpsCopilotPanel } from "@/components/copilot/DevOpsCopilotPanel";
import { useAuthStore } from "@/lib/auth-store";
import { useCopilotStore } from "@/lib/copilot-store";
import { logout } from "@/api/auth";

export function ProjectsLayout() {
  const session = useAuthStore((s) => s.session);
  const { isOpen, toggleCopilot } = useCopilotStore();
  const navigate = useNavigate();

  if (!session) return <Navigate to="/login" replace />;

  const workspace = session.email.split("@")[1]?.split(".")[0] ?? "workspace";

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="sticky top-0 z-40 border-b bg-background/90 backdrop-blur-md">
        <div className="container flex h-14 items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <Link to="/projects" className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <ShieldCheck className="h-4 w-4" />
              </div>
              <span className="hidden text-sm font-semibold sm:inline">Smart AI DevOps</span>
            </Link>
            <span className="rounded-full border bg-muted px-2.5 py-0.5 text-code text-[11px] text-muted-foreground">
              {workspace}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant={isOpen ? "default" : "outline"}
              size="sm"
              onClick={toggleCopilot}
              className={`gap-1.5 shadow-sm transition-all ${
                isOpen
                  ? "bg-primary text-primary-foreground font-semibold"
                  : "border-primary/40 hover:border-primary text-primary hover:bg-primary/10"
              }`}
              title={isOpen ? "Close AI Copilot split view" : "Open AI Copilot split view"}
            >
              <Sparkles className="h-3.5 w-3.5 animate-pulse" />
              <span className="hidden sm:inline font-medium">
                {isOpen ? "Copilot Active" : "AI Copilot"}
              </span>
            </Button>
            <Button size="sm" onClick={() => navigate("/projects/new")}>
              <Plus className="h-3.5 w-3.5" /> New Service
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate("/projects/settings")} title="Settings">
              <Settings className="h-3.5 w-3.5" />
            </Button>
            <HowItWorksDialog />
            <ThemeToggle />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="gap-1.5">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary/15 text-[11px] font-semibold text-primary">
                    {session.email.slice(0, 1).toUpperCase()}
                  </span>
                  <span className="hidden lg:inline">{session.email}</span>
                  <ChevronDown className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>{session.role}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => { logout().finally(() => navigate("/login")); }}>
                  <LogOut className="mr-2 h-3.5 w-3.5" /> Log out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </header>

      <main className="container flex-1 py-6">
        <Outlet />
      </main>

      <footer className="border-t bg-muted/30 py-2.5">
        <div className="container flex flex-wrap items-center justify-between gap-2 text-code text-[11px] text-muted-foreground">
          <span>Smart AI DevOps &amp; Continuous Delivery Platform</span>
          <span>
            Tenant: <span className="font-medium text-foreground">{session.tenant_id}</span>
          </span>
        </div>
      </footer>

      {/* Separate Floating AI Copilot Companion Window */}
      {isOpen && (
        <aside className="fixed top-16 right-4 bottom-4 w-[440px] max-w-[calc(100vw-2rem)] z-50 flex flex-col rounded-2xl border border-border/90 bg-background/98 shadow-2xl overflow-hidden animate-in slide-in-from-right-8 duration-200">
          <DevOpsCopilotPanel />
        </aside>
      )}
    </div>
  );
}
