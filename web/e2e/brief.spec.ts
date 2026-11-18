import { expect, test } from "@playwright/test";
import { ACCESS_CODE } from "../playwright.config";

// The whole journey: login -> submit a brief -> live progress -> the brief with citation chips -> history.
test("build a brief and find it in the history", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/); // no session yet

  await page.getByLabel("Access code").fill(ACCESS_CODE);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Prepare for an interview" })).toBeVisible();
  await page.waitForLoadState("networkidle"); // hydrated, so the form submits through React

  await page.getByLabel("Company").fill("stripe");
  await page.getByLabel("Role").fill("Software Engineer");
  await page.getByText("Intern", { exact: true }).click();
  await page.getByRole("button", { name: "Build brief" }).click();

  // Live progress over SSE
  await expect(page).toHaveURL(/\/briefs\/[0-9a-f]{32}$/);
  await expect(page.getByRole("heading", { name: "Stripe · Software Engineer" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Building your brief" })).toBeVisible();
  await expect(page.getByText(/^\[(search|fetch|extract|ground|coverage|search_more)\]/)).toBeVisible();

  // The finished brief: sections, citation chips that jump to their source, the sources list
  await expect(page.getByRole("button", { name: "Download .md" })).toBeVisible({ timeout: 45_000 });
  await expect(page.getByRole("heading", { name: "TL;DR" })).toBeVisible();
  await expect(page.getByText(/^Intern · United States · built /)).toBeVisible(); // the home page stays mounted (hidden), so be specific
  const chips = page.locator("a.citation");
  expect(await chips.count()).toBeGreaterThan(5);
  const chip = page.getByRole("link", { name: /^Source 2:/ }).first();
  await chip.click();
  await expect(page).toHaveURL(/#source-2$/);
  await expect(page.locator("#source-2")).toBeInViewport();
  await expect(page.locator("#source-2").getByRole("link")).toHaveAttribute("target", "_blank");

  // Download gives the original markdown
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download .md" }).click()]);
  expect(download.suggestedFilename()).toBe("stripe-software-engineer-intern.md");

  // History on the home page
  await page.getByRole("link", { name: "New brief" }).click();
  const history = page.getByRole("region", { name: "Recent briefs" });
  await expect(history.getByRole("listitem")).toHaveCount(1);
  await expect(history.getByRole("link", { name: "Stripe · Software Engineer" })).toBeVisible();
  await expect(history.getByText("Ready")).toBeVisible();
  await history.getByRole("link", { name: "Stripe · Software Engineer" }).click();
  await expect(page.getByRole("heading", { name: "TL;DR" })).toBeVisible(); // reopens the finished brief
});
