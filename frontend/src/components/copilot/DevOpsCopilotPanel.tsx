/**
 * frontend/src/components/copilot/DevOpsCopilotPanel.tsx
 *
 * Enterprise AI DevOps Copilot & UI Guide — Side-by-Side Split View Panel.
 *
 * Features:
 * - Docks cleanly on the right side of the screen without dimming or overlaying the main page.
 * - Allows users to interact with forms, repositories, and tabs on the left while reading and chatting with Copilot on the right.
 * - Context-aware: displays active page and repository inspected.
 * - Multi-turn conversational UI (context isolated in active chat session).
 * - Code block rendering with 1-click Copy.
 * - Contextual quick-action buttons for the current page and GitHub repo.
 * - Clear Chat button to instantly wipe conversational context.
 */
import React, { useState, useRef, useEffect } from "react";
import {
  Sparkles,
  Send,
  X,
  Trash2,
  Bot,
  User,
  Copy,
  Check,
  ShieldCheck,
  HelpCircle,
  GitBranch,
  FolderGit2,
  Terminal,
  Layers,
  Activity,
  Cpu,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { useCopilotStore } from "@/lib/copilot-store";

export function DevOpsCopilotPanel() {
  const {
    closeCopilot,
    messages,
    loading,
    sendMessage,
    clearChat,
    suggestedActions,
    wizardContext,
  } = useCopilotStore();

  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;
    const text = input;
    setInput("");
    void sendMessage(text);
  };

  const handleChipClick = (suggestion: string) => {
    if (loading) return;
    void sendMessage(suggestion);
  };

  const activeRepo = (wizardContext?.repo as string) || (wizardContext?.repo_name as string) || "";
  const activeOwner = (wizardContext?.owner as string) || "";
  const activePage = (wizardContext?.page as string) || "";

  return (
    <div className="flex h-full w-full flex-col bg-background select-text">
      {/* Header */}
      <header className="flex h-14 items-center justify-between border-b px-4 bg-muted/20 shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <Sparkles className="h-4 w-4 text-primary animate-pulse" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <h3 className="text-sm font-semibold tracking-tight truncate">DevOps Copilot</h3>
              <Badge variant="outline" className="h-4 px-1.5 text-[10px] font-normal border-primary/30 text-primary shrink-0">
                AI Companion
              </Badge>
            </div>
            <p className="text-[11px] text-muted-foreground truncate">
              {activeRepo ? `Inspecting ${activeOwner ? `${activeOwner}/` : ""}${activeRepo}` : "UI Guide & Onboarding Assistant"}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-muted-foreground hover:text-foreground"
            title="Clear active chat context"
            onClick={clearChat}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-muted-foreground hover:text-foreground"
            title="Close split view"
            onClick={closeCopilot}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </header>

      {/* Context & Guardrail Notice Bar */}
      <div className="flex items-center justify-between gap-2 border-b bg-primary/5 px-3 py-1.5 text-[11px] text-muted-foreground shrink-0">
        <div className="flex items-center gap-1.5 truncate">
          <ShieldCheck className="h-3.5 w-3.5 text-primary shrink-0" />
          <span className="truncate">Strict Guardrails Active • Isolated Chat Session</span>
        </div>
        {activePage && (
          <span className="rounded bg-muted px-1.5 py-0.2 text-[10px] text-foreground shrink-0">
            {activePage}
          </span>
        )}
      </div>

      {/* GitHub Repo Quick Context Indicator (if available) */}
      {activeRepo && (
        <div className="flex items-center gap-2 border-b bg-muted/40 px-3 py-1.5 text-xs text-foreground shrink-0">
          <FolderGit2 className="h-3.5 w-3.5 text-primary shrink-0" />
          <span className="font-medium truncate">Target Repository:</span>
          <span className="font-mono text-[11px] text-primary truncate">
            {activeOwner ? `${activeOwner}/` : ""}{activeRepo}
          </span>
          <GitBranch className="h-3 w-3 text-muted-foreground ml-auto shrink-0" />
          <span className="text-[11px] text-muted-foreground shrink-0">
            {(wizardContext?.branch as string) || "main"}
          </span>
        </div>
      )}

      {/* Message Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4 min-h-0">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center p-4 space-y-4 text-muted-foreground">
            <div className="flex h-11 w-11 items-center justify-center rounded-full bg-primary/10 text-primary">
              <Bot className="h-5 w-5" />
            </div>
            <div className="space-y-1">
              <h4 className="text-sm font-medium text-foreground">DevOps Copilot Split Assistant</h4>
              <p className="text-xs max-w-xs leading-relaxed">
                I can inspect your GitHub repository, guide you through every input on this page, or explain platform verifications side-by-side.
              </p>
            </div>

            {/* Context-aware suggestions */}
            <div className="w-full space-y-2 pt-2 text-left">
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">
                Recommended for this page:
              </p>
              <div className="grid grid-cols-1 gap-2">
                {activeRepo ? (
                  <>
                    <button
                      onClick={() => handleChipClick(`Inspect my GitHub repository "${activeOwner ? `${activeOwner}/` : ""}${activeRepo}" and tell me which container port, start command, and build command to use.`)}
                      className="flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 p-2.5 text-xs text-foreground hover:bg-primary/10 transition-colors text-left"
                    >
                      <FolderGit2 className="h-4 w-4 text-primary shrink-0" />
                      <span>Inspect <strong>{activeRepo}</strong> on GitHub and suggest port &amp; commands</span>
                    </button>
                    <button
                      onClick={() => handleChipClick(`For service "${activeRepo}", should I choose Canary or Blue-Green deployment mode?`)}
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 p-2.5 text-xs text-foreground hover:bg-muted transition-colors text-left"
                    >
                      <Layers className="h-4 w-4 text-primary shrink-0" />
                      <span>Canary vs Blue-Green recommendation for <strong>{activeRepo}</strong></span>
                    </button>
                    <button
                      onClick={() => handleChipClick(`How should I configure Path Prefix for "${activeRepo}" on the AWS Application Load Balancer?`)}
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 p-2.5 text-xs text-foreground hover:bg-muted transition-colors text-left"
                    >
                      <Cpu className="h-4 w-4 text-primary shrink-0" />
                      <span>How to configure Path Prefix for <strong>{activeRepo}</strong></span>
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      onClick={() => handleChipClick("Which container port should I select for my Node.js or Python app?")}
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 p-2.5 text-xs text-foreground hover:bg-muted transition-colors text-left"
                    >
                      <Terminal className="h-4 w-4 text-primary shrink-0" />
                      <span>Which container port should I select for my app?</span>
                    </button>
                    <button
                      onClick={() => handleChipClick("How do I choose between Canary and Blue-Green deployment mode?")}
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 p-2.5 text-xs text-foreground hover:bg-muted transition-colors text-left"
                    >
                      <Layers className="h-4 w-4 text-primary shrink-0" />
                      <span>How do I choose between Canary and Blue-Green?</span>
                    </button>
                    <button
                      onClick={() => handleChipClick("Explain what each status badge (QUEUED, BUILDING, VERIFYING, PROMOTED) means on the UI")}
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 p-2.5 text-xs text-foreground hover:bg-muted transition-colors text-left"
                    >
                      <Activity className="h-4 w-4 text-primary shrink-0" />
                      <span>Explain what each status badge means on the UI</span>
                    </button>
                  </>
                )}
              </div>
            </div>
          </div>
        ) : (
          messages.map((msg, index) => (
            <div
              key={index}
              className={`flex gap-2.5 text-xs leading-relaxed ${
                msg.role === "user" ? "justify-end" : "justify-start"
              }`}
            >
              {msg.role === "assistant" && (
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/15 text-primary mt-0.5">
                  <Bot className="h-4 w-4" />
                </div>
              )}
              <div
                className={`max-w-[88%] rounded-lg p-3 ${
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground font-normal"
                    : "bg-muted/50 border border-border/80 text-foreground"
                }`}
              >
                <MessageBody content={msg.content} isUser={msg.role === "user"} />
              </div>
              {msg.role === "user" && (
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground mt-0.5">
                  <User className="h-4 w-4" />
                </div>
              )}
            </div>
          ))
        )}

        {/* Loading Indicator */}
        {loading && (
          <div className="flex gap-2.5 text-xs justify-start items-center">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/15 text-primary">
              <Sparkles className="h-4 w-4 animate-spin" />
            </div>
            <div className="rounded-lg bg-muted/50 border border-border/80 p-2.5 text-muted-foreground flex items-center gap-1.5">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-primary animate-bounce" />
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-primary animate-bounce [animation-delay:0.2s]" />
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-primary animate-bounce [animation-delay:0.4s]" />
              <span className="ml-1 text-[11px]">Inspecting platform &amp; GitHub context...</span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Suggested Follow-up Chips */}
      {suggestedActions && suggestedActions.length > 0 && !loading && (
        <div className="border-t bg-muted/10 p-2.5 overflow-x-auto shrink-0">
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground mb-1.5">
            <HelpCircle className="h-3 w-3" />
            <span>Suggested questions:</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {suggestedActions.map((action, idx) => (
              <button
                key={idx}
                onClick={() => handleChipClick(action)}
                className="rounded-full border border-border bg-background px-2.5 py-1 text-[11px] text-foreground hover:bg-muted hover:border-primary/50 transition-colors text-left truncate max-w-full"
              >
                {action}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input Box */}
      <footer className="border-t p-3 bg-background shrink-0">
        <form onSubmit={handleSubmit} className="flex items-center gap-2">
          <Input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about this page, GitHub repo, ports, commands..."
            className="h-9 text-xs focus-visible:ring-1"
            disabled={loading}
          />
          <Button
            type="submit"
            size="sm"
            className="h-9 px-3 gap-1"
            disabled={loading || !input.trim()}
          >
            <Send className="h-3.5 w-3.5" />
          </Button>
        </form>
      </footer>
    </div>
  );
}

/**
 * Clean markdown-like message body renderer supporting code blocks with Copy buttons,
 * tables, bold headers, inline backticks, and bullet points.
 */
function MessageBody({ content, isUser }: { content: string; isUser: boolean }) {
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  const copyToClipboard = (text: string, index: number) => {
    void navigator.clipboard.writeText(text);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  if (isUser) {
    return <span>{content}</span>;
  }

  // Split by code blocks ```...```
  const parts = content.split(/(```[\s\S]*?```)/g);

  return (
    <div className="space-y-2">
      {parts.map((part, index) => {
        if (part.startsWith("```") && part.endsWith("```")) {
          // Code block
          const lines = part.slice(3, -3).trim().split("\n");
          const firstLine = lines[0].trim();
          const isLang = /^[a-zA-Z0-9_-]+$/.test(firstLine);
          const lang = isLang ? firstLine : "";
          const code = (isLang ? lines.slice(1) : lines).join("\n");

          return (
            <div key={index} className="relative my-2 rounded-md bg-black/90 text-white font-mono text-[11px] overflow-hidden border border-border/40">
              <div className="flex items-center justify-between px-3 py-1 bg-white/5 border-b border-white/10 text-[10px] text-gray-400">
                <span>{lang || "code"}</span>
                <button
                  onClick={() => copyToClipboard(code, index)}
                  className="flex items-center gap-1 hover:text-white transition-colors"
                >
                  {copiedIndex === index ? (
                    <>
                      <Check className="h-3 w-3 text-emerald-400" />
                      <span className="text-emerald-400">Copied</span>
                    </>
                  ) : (
                    <>
                      <Copy className="h-3 w-3" />
                      <span>Copy</span>
                    </>
                  )}
                </button>
              </div>
              <pre className="p-3 overflow-x-auto whitespace-pre">
                <code>{code}</code>
              </pre>
            </div>
          );
        }

        // Check if part contains markdown table
        if (part.includes("|") && part.includes("---")) {
          return <TableOrText key={index} text={part} />;
        }

        // Paragraph with basic inline formatting
        const lines = part.split("\n");
        return (
          <div key={index} className="space-y-1">
            {lines.map((line, lIdx) => {
              const trimmed = line.trim();
              if (!trimmed) return <div key={lIdx} className="h-1" />;

              // Headers
              if (trimmed.startsWith("### ")) {
                return <h5 key={lIdx} className="font-semibold text-[13px] text-foreground pt-1">{trimmed.replace("### ", "")}</h5>;
              }
              if (trimmed.startsWith("## ")) {
                return <h4 key={lIdx} className="font-semibold text-sm text-foreground pt-1">{trimmed.replace("## ", "")}</h4>;
              }
              if (trimmed.startsWith("# ")) {
                return <h3 key={lIdx} className="font-bold text-sm text-foreground pt-1">{trimmed.replace("# ", "")}</h3>;
              }

              // List items
              if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
                return (
                  <div key={lIdx} className="flex items-start gap-1.5 pl-1.5">
                    <span className="text-primary mt-1">•</span>
                    <span>{renderInline(trimmed.slice(2))}</span>
                  </div>
                );
              }

              return <p key={lIdx}>{renderInline(line)}</p>;
            })}
          </div>
        );
      })}
    </div>
  );
}

/** Render basic markdown table or fallback to lines */
function TableOrText({ text }: { text: string }) {
  const lines = text.trim().split("\n");
  const isTable = lines.length >= 2 && lines[0].includes("|") && lines[1].includes("---");

  if (!isTable) {
    return <p>{renderInline(text)}</p>;
  }

  const headerCells = lines[0].split("|").filter((_, i, arr) => i !== 0 && i !== arr.length - 1).map((c) => c.trim());
  const bodyRows = lines.slice(2).filter((l) => l.includes("|")).map((l) =>
    l.split("|").filter((_, i, arr) => i !== 0 && i !== arr.length - 1).map((c) => c.trim())
  );

  return (
    <div className="overflow-x-auto my-2 rounded border border-border">
      <table className="w-full text-left text-[11px]">
        <thead className="bg-muted/60 border-b border-border text-foreground font-semibold">
          <tr>
            {headerCells.map((h, i) => (
              <th key={i} className="p-1.5">{renderInline(h)}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {bodyRows.map((row, rIdx) => (
            <tr key={rIdx} className="hover:bg-muted/20">
              {row.map((cell, cIdx) => (
                <td key={cIdx} className="p-1.5">{renderInline(cell)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Render inline bold (**text**) and code (`code`) */
function renderInline(text: string): React.ReactNode {
  const segments = text.split(/(\*\*.*?\*\*|`.*?`)/g);

  return segments.map((seg, i) => {
    if (seg.startsWith("**") && seg.endsWith("**")) {
      return <strong key={i} className="font-semibold text-foreground">{seg.slice(2, -2)}</strong>;
    }
    if (seg.startsWith("`") && seg.endsWith("`")) {
      return (
        <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[11px] text-primary border border-border/50">
          {seg.slice(1, -1)}
        </code>
      );
    }
    return seg;
  });
}
