/**
 * frontend/e2e/onboarding.spec.ts
 *
 * Onboarding a new service through the UI, against the real running stack
 * (api-gateway -> pipeline-worker -> real Kubernetes manifests), not a mock.
 * Uses a randomized service name per run so repeat runs don't collide on the
 * unique project/pipeline name.
 *
 * Rewritten when the classic /app console was retired: the separate "Add
 * Service" dialog it hosted was superseded by the 3-step project wizard,
 * which covers the same fields (image, tags, port, health path, traffic
 * prefix) plus the repository, build/test commands and verification policy.
 */
import { expect, test } from "@playwright/test";

test.describe("onboarding: create a service through the project wizard", () => {
  test("creates a real project and lands in its workspace", async ({ page }) => {
    const serviceName = `e2e-svc-${Date.now()}`;

    await page.goto("/login");
    await page.getByLabel(/email/i).fill("demo@acme-corp.test");
    await page.getByLabel(/password/i).fill("acme-demo-2026");
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page).toHaveURL(/\/projects/);

    await page.getByRole("button", { name: /new service/i }).first().click();
    await expect(page).toHaveURL(/\/projects\/new/);

    // Step 1 — a public clone URL, so the test needs no GitHub credentials.
    await page.getByRole("button", { name: /public git repository/i }).click();
    await page.getByPlaceholder("https://github.com/org/repo").fill("https://github.com/octocat/Hello-World");
    await page.getByRole("button", { name: /use this/i }).click();
    await page.getByRole("button", { name: /continue/i }).click();

    // Step 2 — identity + registry + networking.
    await page.getByLabel(/service name/i).fill(serviceName);
    await page.getByLabel(/container image registry/i).fill("localhost:5001/payments");
    await page.getByRole("button", { name: /continue/i }).click();

    // Step 3 — accept the default canary policy and deploy. Cluster
    // provisioning is best-effort, so the project is created either way.
    await page.getByRole("button", { name: /deploy service & start canary loop/i }).click();

    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: serviceName })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: /verification inspector/i })).toBeVisible();
  });
});
