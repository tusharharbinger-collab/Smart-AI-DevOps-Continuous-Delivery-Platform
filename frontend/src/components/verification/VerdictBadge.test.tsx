import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { VerdictBadge } from "./VerdictBadge";

describe("VerdictBadge", () => {
  it("renders the status text", () => {
    render(<VerdictBadge status="HEALTHY" />);
    expect(screen.getByText(/HEALTHY/)).toBeInTheDocument();
  });

  it("renders confidence as a percentage when provided", () => {
    render(<VerdictBadge status="FAILED" confidence={0.913} />);
    expect(screen.getByText(/91%/)).toBeInTheDocument();
  });

  it("omits confidence when not provided", () => {
    render(<VerdictBadge status="DEGRADED" />);
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });
});
