# Design System — Smart AI DevOps & Continuous Delivery Platform

A B2B delivery-ops console: pipeline orchestration, statistical verification
evidence, policy gates, and audit history. Dense, data-forward, and
utilitarian — closer to a monitoring/observability tool (Grafana, Datadog)
than a marketing product. No illustration, no decorative imagery.

## Stack
- React + TypeScript + Vite
- Tailwind CSS, CSS-variable-driven theme (HSL triples), `darkMode: "class"`
- shadcn/ui component primitives on top of Radix UI (`@radix-ui/react-*`)
- `lucide-react` for icons (line icons only, no filled/duotone)
- `recharts` for charts
- `sonner` for toasts

## Color tokens
All colors are HSL triples on CSS custom properties (`--background: 222
47% 11%` etc.), consumed via Tailwind's `hsl(var(--x))` pattern — never a
hardcoded hex in a component. Full light/dark pairs:

| Token | Light | Dark |
|---|---|---|
| `background` | `0 0% 100%` | `222 47% 8%` |
| `foreground` | `222 47% 11%` | `210 40% 98%` |
| `card` | `0 0% 100%` | `222 40% 11%` |
| `primary` | `221 83% 53%` (blue) | `217 91% 60%` |
| `primary-foreground` | `210 40% 98%` | `222 47% 11%` |
| `secondary` / `muted` / `accent` | `210 40% 96%` / `96%` / `94%` | `217 33% 17%` / `17%` / `20%` |
| `destructive` | `0 72% 51%` (red) | `0 63% 45%` |
| `success` | `142 71% 35%` (green) | `142 71% 45%` |
| `warning` | `38 92% 50%` (amber) | `38 92% 55%` |
| `border` / `input` / `ring` | `214 32% 91%` | `217 33% 20%` |

`success`/`warning` are custom additions on top of shadcn's default palette
— this domain needs them constantly (verdict status, run status, gate
state) and they're first-class tokens, not one-off inline colors.

Semantic mapping used throughout: **HEALTHY/COMPLETED/success → green,
FAILED/ROLLED_BACK/destructive → red, RUNNING/PAUSED/AWAITING_APPROVAL →
amber, DEGRADED/unknown → neutral/secondary badge.**

## Typography
No custom font is loaded — the system UI font stack (Tailwind's default
`font-sans`) is used everywhere, deliberately. This is a tool people live
in for hours at a time; native OS font rendering over a webfont keeps it
fast and unremarkable rather than "designed." `font-feature-settings:
"cv02","cv03","cv04","cv11"` is enabled globally for clearer numeral
rendering (matters a lot here — the UI is full of percentages, p-values,
confidence scores, sample counts).

Scale is Tailwind's default `text-xs` (11–12px, the dominant size — nav,
badges, table cells, metadata) through `text-sm` (body/labels) up to
`text-lg`/`text-xl` for card titles only. Nothing larger — this is not a
marketing surface.

## Spacing & radius
- `--radius: 0.5rem` (8px) — the single radius token; `lg`/`md`/`sm`
  variants are derived from it (`calc(var(--radius) - 2px)` etc.), never
  redeclared per-component.
- Layout container: centered, `1rem` padding, standard Tailwind breakpoints.
- Card padding via shadcn's `Card`/`CardHeader`/`CardContent` primitives —
  never ad hoc `p-4`/`p-6` on a raw `div` standing in for a card.

## Iconography
`lucide-react` exclusively, at `h-3.5 w-3.5` (14px) in dense contexts —
nav, badges, buttons — and `h-4 w-4`/`h-8 w-8` only for section headers or
empty-state illustrations. Never a filled icon set, never an emoji as a UI
icon (emoji only appear inside Slack alert text, which is a different
surface entirely).

## Components (shadcn/ui — do not hand-roll a replacement)
`Button`, `Card`, `Select`, `DropdownMenu`, `AlertDialog`, `Skeleton`,
`Badge` (with `default | success | destructive | secondary | warning`
variants — see `TrafficGauge.tsx`'s `STATUS_VARIANT` map for the canonical
status→variant mapping to reuse), plus a custom `HelpTooltip` for inline
statistical-term explanations (composite score, confidence, tier-1 breach)
that appear throughout Verification Inspector and Policy & Gates.

## Layout patterns actually in use
- **App shell** (`AppLayout.tsx`): fixed header (product name + trigger
  button + user menu) → nav tabs (`Pipeline View`, `Verification
  Inspector`, `Policy & Gates`, `Audit Ledger`) → a context bar (pipeline
  picker, run picker, "Add Service" action) → `<Outlet>` content area.
- **Screen body**: `grid grid-cols-1 md:grid-cols-3 gap-4` — a 2-col-wide
  primary card + 1-col-wide secondary card is the standard split (e.g.
  rollout progress + live log).
- **Stepper** (`PipelineDAG.tsx`): horizontal, clickable stage chips joined
  by `→`, 4 visual states (pending/neutral, running/pulsing-primary,
  done/green-check, failed/red-x). Clicking a stage filters the adjacent
  log panel — interaction lives in the chip itself, not a separate control.
- **Live log panel**: fixed-height (`h-72`), dark slate background
  (`bg-slate-950`) with green monospace text regardless of light/dark app
  theme — a terminal-style panel is intentionally its own visual register.
- **Charts** (`recharts`): area chart with a primary-color gradient fill
  for the canary traffic-weight ramp; small inline gauges (`ConfidenceGauge`,
  `TrafficGauge`) rather than full chart real estate for single-value
  metrics.
- **Empty/loading states**: `Skeleton` blocks matching the eventual
  layout's grid shape (never a spinner-in-a-box), and a centered `Card`
  with a muted icon + one line of copy for "nothing here yet" states —
  never a blank screen.

## What NOT to introduce
- No hardcoded hex colors — always the CSS variable tokens above.
- No new font family.
- No illustration/marketing imagery — this product has none today by
  design (see MASTER_BUILD_SPEC.md's constraint against building "an
  open-ended generic SaaS PaaS or marketing landing page").
- No filled icons, no emoji-as-UI-icon.
- No spinner components — `Skeleton` for loading, pulsing `CircleDot` icon
  for an in-progress step.

## Assets
No logo, custom font files, or image assets exist in this repository —
the product is currently text/icon/chart only (see `index.html`'s bare
`<title>`, no favicon, no `<link>` font imports). Anyone extending this
with a real logo should add it as an SVG (matches the icon set's vector,
theme-aware nature) rather than a raster PNG.
