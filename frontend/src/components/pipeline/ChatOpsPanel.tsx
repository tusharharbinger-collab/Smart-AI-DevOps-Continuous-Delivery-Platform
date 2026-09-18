/**
 * frontend/src/components/pipeline/ChatOpsPanel.tsx
 *
 * ChatOps query interface — assignment bonus item ("a query interface that
 * still grounds its answer in the real comparison data"). Every answer is
 * grounded entirely in this project's real recent runs (see
 * api/chatops.ts's docstring for the backend chain) — the confidence badge
 * and cited run ids are shown as-is, never hidden, so a low-confidence
 * "insufficient data" answer reads honestly rather than looking like a
 * normal one.
 */
import { useState } from "react";
import { MessageCircleQuestion, Loader2, Send } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { askProjectQuestion, type ChatOpsAnswer } from "@/api/chatops";

interface ChatOpsPanelProps {
  projectId: string;
}

interface Exchange {
  question: string;
  answer?: ChatOpsAnswer;
}

const SUGGESTED_QUESTIONS = [
  "Why did the last run roll back?",
  "What's our rollback rate recently?",
  "What actions were taken on the most recent run?",
];

export function ChatOpsPanel({ projectId }: ChatOpsPanelProps) {
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [asking, setAsking] = useState(false);

  async function ask(q: string) {
    const trimmed = q.trim();
    if (!trimmed || asking) return;
    setAsking(true);
    setQuestion("");
    setExchanges((prev) => [...prev, { question: trimmed }]);
    try {
      const answer = await askProjectQuestion(projectId, trimmed);
      setExchanges((prev) =>
        prev.map((ex, i) => (i === prev.length - 1 ? { ...ex, answer } : ex))
      );
    } catch {
      setExchanges((prev) =>
        prev.map((ex, i) =>
          i === prev.length - 1
            ? {
                ...ex,
                answer: {
                  answer: "Couldn't reach the ChatOps assistant right now — see the Audit Ledger tab for the same underlying data.",
                  cited_run_ids: [],
                  confidence: "insufficient_data",
                },
              }
            : ex
        )
      );
    } finally {
      setAsking(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 border-b bg-muted/30">
        <CardTitle className="flex items-center gap-1.5">
          <MessageCircleQuestion className="h-3.5 w-3.5 text-muted-foreground" />
          Ask about this project
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-4">
        {exchanges.length === 0 ? (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              Ask a plain-language question — every answer is grounded in this project's real
              run history, never guessed.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {SUGGESTED_QUESTIONS.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => ask(q)}
                  className="rounded-full border border-border/60 bg-background px-2.5 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="max-h-72 space-y-3 overflow-y-auto pr-1">
            {exchanges.map((ex, i) => (
              <div key={i} className="space-y-1.5">
                <div className="ml-auto w-fit max-w-[90%] rounded-lg bg-primary/10 px-3 py-1.5 text-sm text-foreground">
                  {ex.question}
                </div>
                {ex.answer ? (
                  <div className="w-fit max-w-[90%] space-y-1.5 rounded-lg border border-border/60 bg-muted/30 px-3 py-2">
                    <p className="text-sm text-foreground">{ex.answer.answer}</p>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                          ex.answer.confidence === "grounded"
                            ? "bg-success/20 text-success"
                            : "bg-warning/20 text-warning"
                        }`}
                      >
                        {ex.answer.confidence === "grounded" ? "Grounded" : "Insufficient data"}
                      </span>
                      {ex.answer.cited_run_ids.map((runId) => (
                        <span key={runId} className="rounded bg-background px-1.5 py-0.5 text-code text-[10px] text-muted-foreground">
                          run {runId.slice(0, 8)}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="flex w-fit items-center gap-1.5 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" /> Thinking…
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            ask(question);
          }}
        >
          <Input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. why did the last deploy roll back?"
            disabled={asking}
            className="h-8 text-sm"
          />
          <Button type="submit" size="sm" className="h-8 gap-1" disabled={asking || !question.trim()}>
            <Send className="h-3.5 w-3.5" />
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
