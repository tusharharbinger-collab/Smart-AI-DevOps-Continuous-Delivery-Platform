/**
 * frontend/e2e/project-deletion.spec.ts
 *
 * Real gap found live (2026-09-30): deleting a project used to be one opaque spinner with no visibility
 * into what was actually happening, and never tore down an AI-provisioned draft's real extra resources (an
 * S3 bucket, a database, …) or the build's ECR image — both kept billing forever. This drives the real
 * Delete button on Projects Overview against the real running stack: confirmation dialog -> real background
 * job -> real step-by-step progress -> the card disappearing once it's actually gone.
 *
 * Requires a real project named "testing-2" (created against the real testing_2 GitHub repo, AWS ECS,
 * with a linked AI-provisioned S3-bucket infra draft, then a real rollout triggered) to already exist —
 * this spec's job is to delete it, not create it (creation goes through the wizard + AI infra flow, already
 * covered by onboarding.spec.ts).
 */
import { expect, test } from "@playwright/test";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill("demo@acme-corp.test");
  await page.getByLabel(/password/i).fill("acme-demo-2026");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/projects/);
}

test.describe("deleting a project: confirmation, real progress, and the card actually going away", () => {
  test("clicking Delete on testing-2 asks for confirmation, shows real step-by-step progress, and removes the card", async ({ page }) => {
    await login(page);

    await expect(page.getByText("testing-2")).toBeVisible({ timeout: 15_000 });

    // The trash icon is a ghost button with no visible label - "Delete service" is its title attribute.
    await page.getByTitle("Delete service").first().click();

    // Real confirmation dialog, naming exactly what will be torn down.
    await expect(page.getByText(/Permanently delete testing-2\?/)).toBeVisible();
    await expect(page.getByText(/AWS billing for it stops immediately/)).toBeVisible();
    await expect(page.getByText(/AI-provisioned infrastructure/)).toBeVisible();
    await expect(page.getByText(/container image in ECR/)).toBeVisible();

    await page.getByRole("button", { name: "Delete permanently" }).click();

    // The real progress dialog: a step list with real labels, not a generic spinner.
    await expect(page.getByText(/Deleting testing-2…|testing-2 deleted|deletion finished with errors/)).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByText("Stopping AWS ECS services and load balancer routing")).toBeVisible();
    await expect(page.getByText("Unregistering repository webhook")).toBeVisible();
    await expect(page.getByText("Removing project, pipeline and run history")).toBeVisible();

    // Real AWS/DB calls take real time - wait for the job to reach a terminal state (generously, since a
    // CloudFormation stack delete + ECS service teardown + ECR delete all happen for real here). The
    // component only ever renders this button once `job.status` is COMPLETED or FAILED (see
    // ProjectDeleteProgress.tsx) - its mere presence is the terminal-state assertion.
    await expect(page.getByRole("button", { name: "Close", exact: true })).toBeVisible({ timeout: 60_000 });
    await page.getByRole("button", { name: "Close", exact: true }).click();

    // The real end state: the project is actually gone, not just visually hidden - reload and confirm it
    // never comes back (a stale query-cache success would still show it disappear once, then reappear).
    // Scoped to the card's own link (not getByText) since the success toast itself also says "testing-2".
    const projectLink = page.getByRole("link", { name: "testing-2" });
    await expect(projectLink).not.toBeVisible();
    await page.reload();
    await expect(projectLink).not.toBeVisible({ timeout: 10_000 });
  });
});
