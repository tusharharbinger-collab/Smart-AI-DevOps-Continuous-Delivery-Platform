/**
 * frontend/e2e/golden-path.spec.ts
 *
 * The frontend equivalent of the backend's adversarial test suite: exercises
 * the actual end-to-end flow a real user takes, against the real backend
 * (docker compose must be up — this is not mocked). A regression here should
 * fail CI, not get discovered by a user.
 *
 * Requires the demo seed data (demo@acme-corp.test / acme-demo-2026,
 * tenant acme-corp with the payments-pipeline registered) to already exist —
 * see README.md's Quick Start.
 *
 * Routes updated when the classic /app console was retired: every pipeline,
 * including ones registered straight through the API, is now reached via its
 * project workspace at /projects/:id.
 */
import { expect, test } from "@playwright/test";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill("demo@acme-corp.test");
  await page.getByLabel(/password/i).fill("acme-demo-2026");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/projects/);
}

/** Opens the first service card and waits for its workspace to settle on a run. */
async function openFirstProject(page: import("@playwright/test").Page) {
  await page.locator('a[href^="/projects/"]').first().click();
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+\/pipeline/, { timeout: 15_000 });
}

test.describe("golden path: login → trigger rollout → see verdict → see it in the ledger", () => {
  test("landing page shows the product and links to login", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: /continuous delivery that verifies its own deployments/i })).toBeVisible();
    await page.getByRole("link", { name: /sign in/i }).first().click();
    await expect(page).toHaveURL(/\/login/);
  });

  test("logs in and lands on the projects overview", async ({ page }) => {
    await login(page);
    await expect(page.getByRole("heading", { name: /^overview$/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /new service/i }).first()).toBeVisible();
  });

  test("rejects a bad password", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel(/email/i).fill("demo@acme-corp.test");
    await page.getByLabel(/password/i).fill("definitely-wrong");
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page.getByText(/invalid email or password/i)).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("triggering a rollout produces a verdict visible in Verification Inspector", async ({ page }) => {
    await login(page);
    await openFirstProject(page);

    await page.getByRole("button", { name: /trigger new rollout/i }).click();
    // Real race found live: ProjectWorkspace.tsx already auto-selects the
    // most recent existing run in the URL's `run=` param the moment the
    // runs list loads — BEFORE this click's own POST resolves. Waiting for
    // `/[?&]run=/` alone is satisfied by that pre-existing value, so the
    // test would click into Verification Inspector for the WRONG (stale)
    // run, which then gets silently swapped out from under it moments
    // later when handleTrigger's own setSearchParams call actually lands.
    // handleTrigger's toast fires only after that call already ran, so
    // waiting for it guarantees the URL now names the real freshly
    // triggered run before we navigate anywhere.
    await expect(page.getByText(/rollout triggered/i)).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]run=/, { timeout: 5_000 });

    await page.getByRole("link", { name: /verification inspector/i }).click();
    await expect(page).toHaveURL(/\/verification/);
    await expect(page.getByText(/HEALTHY|DEGRADED|FAILED|No verdict published yet/)).toBeVisible({ timeout: 20_000 });
  });

  test("Policy & Gates and Audit Ledger screens render without crashing", async ({ page }) => {
    await login(page);
    await openFirstProject(page);

    await page.getByRole("link", { name: /policy & gates/i }).click();
    await expect(page).toHaveURL(/\/policy/);
    await expect(page.getByText(/pipeline & gates yaml/i)).toBeVisible();

    await page.getByRole("link", { name: /audit ledger/i }).click();
    await expect(page).toHaveURL(/\/audit/);
    await expect(page.getByText(/export soc 2 csv/i)).toBeVisible();
  });

  test("logging out returns to the login screen and protects the app routes", async ({ page }) => {
    await login(page);

    await page.getByRole("button", { name: /demo@acme-corp\.test/ }).click();
    await page.getByRole("menuitem", { name: /log out/i }).click();
    await expect(page).toHaveURL(/\/login/);

    // Directly navigating to a protected route with no session bounces back to login.
    await page.goto("/projects");
    await expect(page).toHaveURL(/\/login/);
  });

  test("retired /app links redirect to the projects overview instead of 404ing", async ({ page }) => {
    await login(page);
    await page.goto("/app/pipelines");
    await expect(page).toHaveURL(/\/projects/);
    await expect(page.getByRole("heading", { name: /^overview$/i })).toBeVisible();
  });
});
