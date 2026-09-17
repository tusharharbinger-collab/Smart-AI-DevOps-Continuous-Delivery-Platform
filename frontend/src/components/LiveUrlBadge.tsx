/**
 * frontend/src/components/LiveUrlBadge.tsx
 *
 * Real gap this closes: the platform could report a project's live_url
 * without ever having actually confirmed it responds — see
 * shared/live_url_check.py's module docstring. Shared between
 * ProjectsOverview.tsx's card grid and ProjectWorkspace.tsx's header so
 * both read the same status the exact same way, rather than two copies
 * that could drift (the same discipline this codebase already applies to
 * actuation logic).
 */
import { useState } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";

export function LiveUrlBadge({
  liveUrl,
  status,
}: {
  liveUrl: string | null;
  status: "verified" | "failed" | null;
}) {
  const [copied, setCopied] = useState(false);
  if (!liveUrl) return null;

  async function handleCopy(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(liveUrl as string);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API can throw in an insecure context or older browser —
      // the link itself is still right there for the user to copy by hand.
    }
  }

  // Still always a clickable link regardless of status — a failed *check*
  // isn't proof the user can't debug it themselves by visiting the URL.
  const statusStyles =
    status === "verified"
      ? "border-success/30 bg-success/10 text-success"
      : status === "failed"
        ? "border-destructive/30 bg-destructive/10 text-destructive"
        : "border-warning/30 bg-warning/10 text-warning";
  const statusLabel =
    status === "verified" ? "Live · verified" : status === "failed" ? "Verification failed" : "Live · unverified";

  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 ${statusStyles}`}>
      <a
        href={liveUrl}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="inline-flex items-center gap-1 hover:underline"
      >
        {statusLabel} <ExternalLink className="h-2.5 w-2.5" />
      </a>
      <button
        type="button"
        onClick={handleCopy}
        title="Copy live URL"
        className="opacity-70 transition-opacity hover:opacity-100"
      >
        {copied ? <Check className="h-2.5 w-2.5" /> : <Copy className="h-2.5 w-2.5" />}
      </button>
    </span>
  );
}
