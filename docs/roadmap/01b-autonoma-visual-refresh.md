# Phase 1b — "Autonoma" Visual Refresh (re-skin of Phase 1)

**Status: core re-skin done (tokens, layout, all 4 authenticated screens, landing, login, onboarding dialog); fleet-overview table and live Playwright e2e verification still open.**

## Goal
Re-skin every existing screen to match the visual language of the `autonoma-devops/` Stitch-generated mockup (dark navy Material-3-inspired design system, dense data-ops aesthetic, Material-Symbols-style iconography, DAG/telemetry-heavy layouts) — **without** changing the tech stack, the routing model, or any real data flow the app already has. This is a re-skin, not a rewrite: same React Router pages, same hooks, same API calls, same Zustand stores, same Radix/shadcn component primitives — new visual surface on top.

## Why this exists as its own file, not an edit to `01-frontend-overhaul.md`
Phase 1 is marked done and verified (9 unit + 6 e2e tests passing against the real backend). This is a follow-on visual-direction change requested after that phase shipped, not a reopening of its scope. Keeping it as `01b` preserves Phase 1's record of what was verified, while tracking this re-skin's own checklist and — critically — the list of things the mockup depicts that are **not backed by real data**, which must not be reproduced as fact in the shipped product.

## Source of the target design
`autonoma-devops/` at the repo root (a separate, throwaway Vite+React 19+Tailwind 4 app pulled from a Stitch project) contains 7 fully-mocked screens: `LandingPage`, `Navbar`, `DashboardView` (fleet table), `PipelineExecutionView` (single-run DAG + logs), `VerificationInspectorView`, `PolicyAndGatesView`, `AuditLedgerView`, plus `NewServiceModal` and `AuthView`. **All of it is `useState` + hardcoded mock data (`src/data/mockData.ts`) — zero real API calls, zero backend.** It exists purely as a visual/UX reference. It is not integrated as a dependency, its `package.json` (React 19, Tailwind 4, `@google/genai`, `motion`, `express`) is never installed into `frontend/`, and it can be deleted once this phase is done extracting its design language.

## Hard rule: reuse the look, never fabricate the data
The mockup invents specific, precise-looking facts that have no backing system in this codebase: "SOC 2 Type II Certified", "49,120 Merkle blocks", "Sigstore Rekor transparency log", "Ed25519 hardware-key signatures", "4,200+ canary rollouts", a live "Kube-Prod: Connected" pill, a cluster/workspace switcher, GitHub-webhook "pre-verified" badges. None of these exist in the real system (which does real HMAC-SHA256 verdict signing and Postgres-backed audit, not Merkle trees or Sigstore). **Reproduce the visual pattern (a compliance-style summary card, a signature badge, a status pill) but drive its numbers from real data (`useAuditLog`, `useVerificationResult`, actual HMAC prefixes from `audit_events`) or omit the card entirely if there's nothing real to show.** Never ship a static "SOC 2 Type II Certified" badge — this project has not been audited.

## Design-system extraction (token layer — do first, touches the fewest files)
`frontend/src/index.css` already uses the shadcn pattern: HSL CSS variables (`--background`, `--foreground`, `--primary`, `--secondary`, `--muted`, `--accent`, `--destructive`, `--success`, `--warning`, `--border`, `--input`, `--ring`) consumed via `tailwind.config.js`'s `theme.extend.colors`, switched by a `.dark` class (`src/lib/theme.ts` + `<ThemeToggle>`). This is the same mechanism the mockup's `@theme` Tailwind-4 block conceptually maps onto — **no new theming architecture needed**, just retint the existing variables:
- [ ] `.dark` block → Autonoma's navy palette (`surface #0b1326`, primary `#adc6ff`/`#4d8eff`, secondary(success) `#4edea3`, tertiary(warning) `#ffb95f`, error `#ffb4ab`/`#93000a`), mapped onto the existing token names (`--background`, `--primary`, `--success`, `--warning`, `--destructive`, `--muted`, `--card`, `--border`).
- [ ] `:root` (light) block → a light derivative using the same hues (not a literal copy of the mockup, which is dark-only) so `ThemeToggle` keeps working both ways.
- [ ] Add the mockup's density-oriented type scale as Tailwind utility classes in `index.css` `@layer utilities` (e.g. `.text-stat` for the mono stat-display style, tightened `text-xs`/`text-[11px]` code-style labels) rather than adopting Geist/JetBrains Mono as new font files — keep the existing font stack (no new font dependency), but do add a monospace utility for stat/code numbers where the mockup uses `font-mono`.
- [ ] Icons stay **lucide-react** (already a dependency) — map each Material-Symbols icon used in the mockup to its closest lucide equivalent (e.g. `hub`→`Network`, `verified`→`ShieldCheck`, `functions`→`Sigma`, `terminal`→`Terminal`, `gavel`→`Gavel`, `savings`→`PiggyBank`). No Google Fonts icon dependency added.
- [ ] Radix primitives stay for anything interactive (dropdowns, dialogs, selects) — do not hand-roll `useState`-toggled `<div>` dropdowns like the mockup does; re-skin the existing `DropdownMenu`/`Select`/`Dialog` components instead.

## Screen-by-screen mapping (existing file → mockup reference → what changes)

- [x] **Design tokens** (`src/index.css`) — `.dark`/`:root` HSL variables retinted to the Autonoma navy/blue/green/amber palette; added `.text-stat`/`.text-code` mono utilities (existing font stack, no new dependency).
- [x] **`layouts/AppLayout.tsx`** ← `Navbar.tsx` + `Footer.tsx` — restyle the header to the dark fixed-nav look (logo lockup, pill-style nav links, "New Service" button opening the existing `AddServiceDialog`), keep all real logic (pipeline/run `Select`s backed by `/api/v1/pipelines`, trigger-rollout, theme toggle, session dropdown/logout) untouched. Add a slim footer bar (cluster/version labels — use real values already available, e.g. tenant id, not fabricated ones). Notifications dropdown, if added, must be sourced from real recent `useAuditLog` entries, not invented alerts.
- [x] **`pages/Landing.tsx`** ← `LandingPage.tsx` — adopt the hero layout, gradient/grid backdrop, feature-card grid, and code-snippet panel; keep the real 4-step "how it works" copy (already honest) instead of the mockup's fabricated proof-point strip ("4,200+ canary rollouts").
- [x] **`pages/Login.tsx`** ← `AuthView.tsx` — adopted the split-panel layout (brand/status panel + form panel); kept the real `login()` call and error handling. Dropped the mockup's fake GitHub/Google SSO buttons entirely rather than shipping disabled decoys — no SSO backend exists.
- [x] **`pages/PipelineDashboard.tsx`** ← `PipelineExecutionView.tsx` — restyled the header/status strip and log console (dark terminal panel, streaming indicator) around the real DAG/gauge/chart components. **Not done:** the fleet-wide "all pipelines" table (`DashboardView.tsx` reference) — would need the `pipelines` list threaded from `AppLayout` into context; deferred as a follow-up, not fabricated in its place.
- [x] **`pages/VerificationInspector.tsx`** ← `VerificationInspectorView.tsx` — adopted the gauge + stat-strip hero layout, generic across metric categories (verdict ID / metrics-evaluated count / sample floor, not hardcoded test names). `EvidencePanel`'s existing per-category rendering untouched.
- [x] **`pages/PolicyManager.tsx`** ← `PolicyAndGatesView.tsx` — restyled card chrome and guardrail rows as mono stat readouts; CodeMirror editor and read-only derived-value cards unchanged in behavior (still the single source of truth is the YAML text, no parallel editable sliders).
- [x] **`pages/AuditLedger.tsx`** ← `AuditLedgerView.tsx` — added 3 real compliance-summary cards (total actions/promotions/rollbacks, signature scheme, most recent entry — all computed from live `entries`) and expandable rows showing each entry's real JSON. No Merkle-tree affordance added (none exists in the backend).
- [x] **`components/onboarding/AddServiceDialog.tsx`** ← `NewServiceModal.tsx` — adopted the numbered 3-step stepper chrome around the existing real form fields and `registerService()` call.

## Explicit non-goals
- No migration to React 19, Tailwind 4, or `@google/genai` — `autonoma-devops/`'s `package.json` is not installed into `frontend/`.
- No new routing model — `AppView`-style `useState` switching from the mockup is **not** adopted; React Router URLs stay canonical.
- No fabricated compliance/security claims shipped as static UI copy.
- `autonoma-devops/` itself is a reference-only scratch folder, not a workspace package — safe to delete once extraction is complete.

## Acceptance criteria
- [x] `tsc -b` and `vite build` succeed with no new dependencies beyond what's already in `frontend/package.json` (verified this session — `node_modules/.bin/*.cmd` shims are broken in this checkout's path because it contains an `&`, which `cmd.exe` treats as a command separator; invoke `node node_modules/typescript/bin/tsc -b` / `node node_modules/vite/bin/vite.js build` directly to work around it, unrelated to this phase's changes).
- [x] Vitest unit suite passes unmodified (12/12 — `auth-store.test.ts`, `client.test.ts`, `VerdictBadge.test.tsx`).
- [ ] Playwright golden-path suite (`e2e/golden-path.spec.ts`) re-run against the restyled UI — **not run this session** (needs the full docker-compose stack up); selectors may need updates for restyled markup.
- [ ] Visual review: side-by-side of each restyled screen against its mockup reference for layout/spacing/color fidelity — **not eyeballed in a browser this session**, only compiled/tested.
- [x] Grep-level check: no new static string in the shipped frontend asserts a compliance/security claim ("SOC 2", "Merkle", "Sigstore", "4,200+") that isn't computed from real backend data — confirmed by construction (AuditLedger's summary cards are computed from live `entries`, Landing/Login dropped the mockup's fabricated proof points and fake SSO).
- [ ] Fleet-overview table on Pipeline View (`DashboardView.tsx` reference) — not built; needs `pipelines` list threading into `useAppContext`.
- `autonoma-devops/` reference folder left in place at the repo root for now — safe to delete once a maintainer confirms the extraction above is sufficient.

## Depends on
[01-frontend-overhaul.md](01-frontend-overhaul.md) (done) — this phase only re-skins what it built.
