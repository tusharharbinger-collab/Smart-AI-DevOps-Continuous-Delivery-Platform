/**
 * Real gap found live (2026-09-30): deleting a project used to be one opaque spinner with no visibility
 * into what was actually happening or whether AWS cost had stopped. This covers the real polling loop
 * (getDeleteJobStatus), the per-step rendering, the terminal-state Close gate, and the "onDone fires once,
 * dialog stays open until the user closes it" contract the parent (ProjectsOverview) relies on.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ProjectDeleteProgress } from "./ProjectDeleteProgress";
import * as projectsApi from "@/api/projects";
import type { DeleteJobStatus } from "@/api/projects";

beforeEach(() => {
  vi.restoreAllMocks();
});

function job(overrides: Partial<DeleteJobStatus> = {}): DeleteJobStatus {
  return {
    job_id: "job-1",
    project_id: "proj-1",
    project_name: "testing-2",
    status: "RUNNING",
    steps: [
      { key: "cluster", label: "Stopping AWS ECS services and load balancer routing", status: "pending", detail: null },
      { key: "webhook", label: "Unregistering repository webhook", status: "pending", detail: null },
      { key: "records", label: "Removing project, pipeline and run history", status: "pending", detail: null },
    ],
    pipeline_retained: false,
    ...overrides,
  };
}

describe("ProjectDeleteProgress", () => {
  it("renders every real step from the job and shows the Close button only once terminal", async () => {
    vi.spyOn(projectsApi, "getDeleteJobStatus").mockResolvedValue(job());
    render(<ProjectDeleteProgress projectId="proj-1" jobId="job-1" projectName="testing-2" onDone={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByText("Stopping AWS ECS services and load balancer routing")).toBeInTheDocument();
    expect(screen.getByText("Unregistering repository webhook")).toBeInTheDocument();
    expect(screen.getByText("Removing project, pipeline and run history")).toBeInTheDocument();
    expect(screen.queryByText("Close")).not.toBeInTheDocument();
  });

  it("calls onDone exactly once when the job reaches COMPLETED, and shows the Close button", async () => {
    const onDone = vi.fn();
    const doneJob = job({
      status: "COMPLETED",
      steps: [
        { key: "cluster", label: "Stopping AWS ECS services and load balancer routing", status: "done", detail: null },
        { key: "webhook", label: "Unregistering repository webhook", status: "done", detail: null },
        { key: "records", label: "Removing project, pipeline and run history", status: "done", detail: null },
      ],
    });
    vi.spyOn(projectsApi, "getDeleteJobStatus").mockResolvedValue(doneJob);

    render(<ProjectDeleteProgress projectId="proj-1" jobId="job-1" projectName="testing-2" onDone={onDone} onClose={vi.fn()} />);

    await screen.findByText("Close");
    // Several polls happen while the mock always returns the same terminal job - onDone must still fire once.
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(onDone).toHaveBeenCalledWith(doneJob);
  });

  it("shows the real per-step failure detail and a distinct title when the job FAILS", async () => {
    vi.spyOn(projectsApi, "getDeleteJobStatus").mockResolvedValue(
      job({
        status: "FAILED",
        steps: [
          { key: "cluster", label: "Stopping AWS ECS services and load balancer routing", status: "failed", detail: "pipeline-worker unreachable: ConnectTimeout" },
          { key: "webhook", label: "Unregistering repository webhook", status: "done", detail: null },
          { key: "records", label: "Removing project, pipeline and run history", status: "done", detail: null },
        ],
      })
    );

    render(<ProjectDeleteProgress projectId="proj-1" jobId="job-1" projectName="testing-2" onDone={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByText("testing-2: deletion finished with errors")).toBeInTheDocument();
    expect(screen.getByText("pipeline-worker unreachable: ConnectTimeout")).toBeInTheDocument();
  });

  it("surfaces a retained pipeline as a warning note, not an error", async () => {
    vi.spyOn(projectsApi, "getDeleteJobStatus").mockResolvedValue(
      job({ status: "COMPLETED", pipeline_retained: true })
    );

    render(<ProjectDeleteProgress projectId="proj-1" jobId="job-1" projectName="testing-2" onDone={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByText(/pipeline record was kept/)).toBeInTheDocument();
  });

  it("calls onClose only when the user clicks Close, never on its own", async () => {
    const onClose = vi.fn();
    vi.spyOn(projectsApi, "getDeleteJobStatus").mockResolvedValue(job({ status: "COMPLETED" }));

    render(<ProjectDeleteProgress projectId="proj-1" jobId="job-1" projectName="testing-2" onDone={vi.fn()} onClose={onClose} />);

    const closeButton = await screen.findByText("Close");
    expect(onClose).not.toHaveBeenCalled();
    closeButton.click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
