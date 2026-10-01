/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.3 (Phase A) — the existing-resource browser that
 * replaces the old plain `<select>`. Covers: real status badges (available=good, deleting=bad,
 * modifying=warn), the "Create new" fallback option, and selection callbacks.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ExistingResourceBrowser } from "./ExistingResourceBrowser";
import type { ExistingResourceCandidate } from "@/api/infraDrafts";

const CANDIDATES: ExistingResourceCandidate[] = [
  { id: "db-1", label: "prod-db-1", details: { engine: "postgres", engine_version: "15.4", instance_class: "db.t3.medium", status: "available" } },
  { id: "db-2", label: "prod-db-2", details: { engine: "postgres", instance_class: "db.t3.micro", status: "deleting" } },
  { id: "db-3", label: "prod-db-3", details: { engine: "mysql", status: "modifying" } },
];

describe("ExistingResourceBrowser", () => {
  it("renders every candidate with its real engine/size and a status badge", () => {
    render(<ExistingResourceBrowser slot="database" label="Database" candidates={CANDIDATES} selectedId={undefined} onSelect={vi.fn()} />);

    expect(screen.getByText("prod-db-1")).toBeInTheDocument();
    expect(screen.getByText(/postgres 15\.4/)).toBeInTheDocument();
    expect(screen.getByText(/db\.t3\.medium/)).toBeInTheDocument();
    expect(screen.getByText("available")).toBeInTheDocument();
    expect(screen.getByText("deleting")).toBeInTheDocument();
    expect(screen.getByText("modifying")).toBeInTheDocument();
  });

  it("flags a non-running resource as not recommended to attach", () => {
    render(<ExistingResourceBrowser slot="database" label="Database" candidates={CANDIDATES} selectedId={undefined} onSelect={vi.fn()} />);
    expect(screen.getByText(/Not running — attaching this is not recommended/)).toBeInTheDocument();
  });

  it("calls onSelect with the candidate id when a card is clicked", () => {
    const onSelect = vi.fn();
    render(<ExistingResourceBrowser slot="database" label="Database" candidates={CANDIDATES} selectedId={undefined} onSelect={onSelect} />);

    fireEvent.click(screen.getByText("prod-db-1"));
    expect(onSelect).toHaveBeenCalledWith("db-1");
  });

  it("calls onSelect with an empty string when 'Create new' is clicked", () => {
    const onSelect = vi.fn();
    render(<ExistingResourceBrowser slot="database" label="Database" candidates={CANDIDATES} selectedId="db-1" onSelect={onSelect} />);

    fireEvent.click(screen.getByText("Create new (don't attach an existing one)"));
    expect(onSelect).toHaveBeenCalledWith("");
  });

  it("shows the empty-state message when no candidates were found", () => {
    render(<ExistingResourceBrowser slot="cache" label="Cache" candidates={[]} selectedId={undefined} onSelect={vi.fn()} />);
    expect(screen.getByText("None found — the agent will create one")).toBeInTheDocument();
  });
});
