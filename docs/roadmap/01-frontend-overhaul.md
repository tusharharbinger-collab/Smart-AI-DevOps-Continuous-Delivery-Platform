# Phase 1 — Frontend Overhaul

**Status: done, verified live (6/6 Playwright e2e + 9/9 Vitest unit tests, against the real running backend).**

## Goal
A user who has never seen this project can land on it, understand what it does within seconds, log in, and operate every screen without a walkthrough — and it should look like a funded startup's product, not a hackathon demo.

## Why now
This was called out explicitly as a priority independent of backend correctness — a user's first impression of "is this real" is visual before it's functional.

## Current state (what we upgraded away from)
- Vite + React 18 + TypeScript + Tailwind, no component library — every button/input/card was hand-styled inline.
- No routing library — "screens" were `useState` toggles, not real URLs.
- No landing page — the app went straight to a login form with zero context.
- No design system, no dark mode, errors via `alert()`/`window.confirm()`, session state read ad hoc from `localStorage` in multiple files.

## Deliverables

### 1.1 — Tech stack upgrade
- [x] **React Router** (`react-router-dom` v7) — real URLs per screen (`/app/pipelines`, `/app/verification`, `/app/policy`, `/app/audit`), with the active pipeline/run selection carried as `?pipeline=&run=` query params so a link is shareable to a specific verdict.
- [x] **shadcn/ui + Radix primitives** — copy-in, ownable component source under `src/components/ui/`: Button, Input, Label, Select, Dialog, AlertDialog, DropdownMenu, Tabs, Tooltip, Table, Badge, Card, Skeleton, Switch, Sonner toaster.
- [x] **TanStack Query** — used for all data fetching (verification result, audit log), proper caching/retry/loading states.
- [x] **Zustand** — `src/lib/auth-store.ts` (session, persisted) and `src/lib/theme.ts` (dark mode), replacing scattered `localStorage` reads.
- [x] **Recharts** — built `LatencyComparisonChart`, `SprtBoundaryChart`, `ConfidenceGauge`, `TrafficWeightChart`. **Honest scope note:** these are *not* a true empirical CDF or a full LLR trajectory — the backend's `/verify` evidence only returns summary statistics (medians, final LLR, p-values), not raw per-request samples or an observation-by-observation LLR history. What's built compares the real summary stats visually (baseline vs. canary median, where the final LLR sits between its accept/reject boundaries) — genuinely useful, but a true ECDF/trajectory chart needs a backend evidence-payload change, which is out of this frontend-only phase's scope (each chart component documents this in its own file).
- [x] **Sonner** — toasts for pause/resume/rollback/trigger/export actions; `AlertDialog` replaces a bare confirm for Emergency Rollback.
- [x] **date-fns** installed (available for use; existing `toLocaleString()` calls left as-is where already clear).
- [x] **CodeMirror** (`@uiw/react-codemirror` + `@codemirror/lang-yaml`) for the Policy & Gates editor, as originally specified — replaced the placeholder `<textarea>`.

**Confirmed: no Next.js migration** — stayed on Vite + React Router per the recommendation.

### 1.2 — Information architecture
- [x] **Landing page** (`/`, unauthenticated) — value prop + the 4-step "how it works" flow + Sign in CTA.
- [x] **Real routing** — `AppLayout` (`src/layouts/AppLayout.tsx`) wraps all authenticated screens via `<Outlet context={...} />`; pipeline/run selection lives in the URL.
- [x] **Navigation shell** — a top-nav bar with tenant/user identity, theme toggle, and a "How this works" panel (not a sidebar — top-nav suited the existing 4-screen breadth better than a sidebar would).
- [x] **Onboarding empty states** — "none registered yet" / "no runs yet — trigger one" inline in the pickers; a brand-new tenant is guided toward **Add Service** (§ below) rather than shown a dead end.

### 1.3 — Screen-by-screen polish
- [x] **Pipeline View** — `PipelineDAG` (step indicators with done/current/pending states), `TrafficGauge` + `TrafficWeightChart` (client-accumulated weight-over-time from live WebSocket pushes), a styled log panel, Pause/Resume/Emergency Rollback (the last behind a real `AlertDialog` confirmation).
- [x] **Verification Inspector** — verdict hero with `ConfidenceGauge`, evidence routed to `LatencyComparisonChart`/`SprtBoundaryChart` where the shape matches, a generic metric card fallback otherwise (business-metric contingency tables, saturation CUSUM/BOCPD) instead of a raw JSON dump.
- [x] **Policy & Gates** — real CodeMirror YAML editor with live OPA-backed validation, loads the pipeline's existing policy on selection (previously always started blank).
- [x] **Audit Ledger** — sortable table (time/action/confidence), SOC 2 CSV export with toast feedback.
- [x] **Auth** — polished login screen; full onboarding ("Add Service") flow, see 1.3-bis below.

### 1.3-bis — Onboarding UI shell (overlaps Phase 3)
- [x] `AddServiceDialog` — a 3-step form (name/image → network → review & deploy) wired to a clearly-commented **mocked** `registerService()` (`src/api/services.ts`). Swap its body for the real `POST /api/v1/services` once Phase 3 delivers it; the form/UI does not need to change.

### 1.4 — Design system fundamentals
- [x] Tailwind theme with CSS-variable tokens (`src/index.css`), light/dark both defined.
- [x] Dark mode via a `.dark` class + Zustand store + `<ThemeToggle>`, persisted and respecting `prefers-color-scheme` on first load.
- [x] `Skeleton` components used for loading states (Pipeline View, Verification Inspector, Audit Ledger).
- [x] Responsive container/grid layout; not stress-tested below ~640px but doesn't break at tablet width.

### 1.5 — Frontend test coverage
- [x] **Vitest** + React Testing Library: `auth-store.test.ts` (3 tests), `client.test.ts` (3 tests — auth header presence, ApiError status propagation), `VerdictBadge.test.tsx` (3 tests). 9/9 passing.
- [x] **Playwright** golden-path suite (`e2e/golden-path.spec.ts`, 6 tests, run against system Chrome since this sandbox has no network access to Playwright's own browser CDN): landing → login (valid and invalid) → trigger a real rollout → see a real verdict in Verification Inspector → visit Policy & Gates and Audit Ledger → log out and confirm protected routes redirect. All 6 passing against the real running stack.

### 1.6 — End-user-facing documentation
- [x] `HelpTooltip` on Confidence, Composite score, and Tier-1 breaches in Verification Inspector.
- [x] `HowItWorksDialog` reachable from the header on every authenticated screen — the same 4-step explanation as the landing page, available without leaving the app.

## Real bugs this phase found and fixed (not just features shipped)
Two genuine, previously-latent bugs surfaced by actually running Playwright against the real app rather than just type-checking:
1. **Toast/button overlap**: the "Signed in" toast (originally top-right) visually sat on top of the "Trigger New Rollout" button, physically blocking clicks for a few seconds after login. Fixed by moving toasts to bottom-right.
2. **Stale-closure query-param clobbering**: `AppLayout`'s auto-select-first-pipeline and auto-select-latest-run effects each called a `setSearchParams`-based setter from a `useCallback` with a narrow dependency array; the run-selection call was invoking a stale version of the pipeline-selection setter's closure, silently dropping the `?pipeline=` query param the moment a run got auto-selected. Fixed with a `useRef` mirror of the current `searchParams`, so the setters always build on top of whatever was actually last committed regardless of which stale closure calls them. Confirmed via Playwright navigating directly to `/app/policy` and `/app/audit` and finding pipeline context correctly preserved.

## Acceptance criteria
- [x] Every screen has a real, bookmarkable URL.
- [x] No `alert()` or `window.confirm()` calls remain in the codebase.
- [x] The Verification Inspector renders actual charts for latency and SPRT (with the summary-stats caveat noted in 1.1), not raw JSON.
- [x] A brand-new user can go from the landing page → login → see a clear next action, with zero prior explanation.
- [x] `npm run build` produces a production bundle with no TypeScript errors, code-split (manualChunks for react/charts/editor/radix — initial single-chunk build exceeded the 500KB warning threshold, now resolved).
- [x] `npm run test` (Vitest, 9/9) and the Playwright golden-path suite (6/6) both pass.

## Depends on
Nothing — done, independent of Phase 2.
