import { expect, test } from "@playwright/test";

import {
  fixture,
  loginAndUnlock,
  selectOwnerOrganisation,
  unlockCurrentPage,
} from "./funnel-helpers";

// This spec is deliberately separate from funnel.spec.ts. It runs against the production Stripe
// adapter with test-mode credentials, while the required PR gate keeps its local billing adapter.

async function fillOptional(
  page: import("@playwright/test").Page,
  selector: string,
  value: string,
) {
  const field = page.locator(selector).first();
  // Stripe may omit optional billing fields depending on the account and payment configuration;
  // the required card fields below remain strict assertions.
  if ((await field.count()) > 0 && (await field.isVisible())) {
    await field.fill(value);
  }
}

// A fresh address every run, because Link remembers them. Checkout offers Link to an email it
// has seen before, and that offer is a verification modal covering the form: the submit button
// is still there, and nothing can reach it. The failure reads as a missing button, which sends
// you looking at Stripe's copy rather than at an overlay.
//
// `.test` is reserved by RFC 2606 and can never be a real domain, so these never leave Stripe.
function unseenEmail(): string {
  return `e2e-stripe-${Date.now()}-${Math.floor(Math.random() * 1e6)}@sotto.test`;
}

test("real Stripe Checkout completes and applies the Team tier", async ({ page }) => {
  await loginAndUnlock(page);
  await selectOwnerOrganisation(page);

  const upgrade = page.getByRole("button", { name: "Upgrade to Team" });
  await expect(upgrade).toBeVisible();
  await upgrade.click();

  // Checkout is hosted by Stripe, so this proves the server handed the browser to the real
  // provider rather than the local e2e-mock-billing page.
  await page.waitForURL(/checkout\.stripe\.com/, { timeout: 60_000 });
  await expect(
    page.locator('input[name="cardNumber"], input[autocomplete="cc-number"]').first(),
  ).toBeVisible({ timeout: 60_000 });

  await fillOptional(page, 'input[name="email"], input[type="email"]', unseenEmail());
  await page
    .locator('input[name="cardNumber"], input[autocomplete="cc-number"]')
    .first()
    .fill("4242 4242 4242 4242");
  await page
    .locator('input[name="cardExpiry"], input[autocomplete="cc-exp"]')
    .first()
    .fill("12/34");
  await page
    .locator('input[name="cardCvc"], input[autocomplete="cc-csc"]')
    .first()
    .fill("123");
  await fillOptional(page, 'input[name="billingName"]', "Sotto E2E");
  await fillOptional(page, 'input[name="billingPostalCode"]', "94107");
  await fillOptional(page, 'input[name="phoneNumber"], input[autocomplete="tel"]', "4155552671");

  // Belt and braces for the same overlay. A new address should never be offered Link, but
  // Checkout decides that at its end and this test has already spent five weeks red; dismissing
  // a prompt that is usually absent costs one call, and not dismissing it costs a two minute
  // timeout and a failure that names the wrong thing.
  const linkPrompt = page.getByRole("button", { name: /^close$/i });
  if (await linkPrompt.isVisible().catch(() => false)) {
    await linkPrompt.click();
  }

  await page.getByRole("button", { name: /Pay|Subscribe|Start trial/i }).click();
  await page.waitForURL(/billing=success/, { timeout: 60_000 });
  await unlockCurrentPage(page);
  await expect(page.getByText("Payment received.")).toBeVisible();

  // Stripe delivers the entitlement asynchronously through the forwarded webhook. Poll the same
  // authenticated endpoint the TeamPanel uses, then reload once to prove the visible plan agrees.
  // Returns what it saw rather than whether it liked it. A boolean here reports `false` for a
  // free tier, a rejected request and a response whose shape changed, which are three different
  // problems; the last failure cost a round trip establishing which. Playwright prints the last
  // polled value, so saying it plainly is free.
  await expect
    .poll(
      async () =>
        page.evaluate(async (orgId) => {
          const response = await fetch(`/orgs/${encodeURIComponent(orgId)}/entitlements`, {
            credentials: "include",
          });
          if (!response.ok) return `http ${response.status}`;
          const body = (await response.json()) as {
            tier?: string;
            effective_tier?: string;
          };
          return `tier=${body.tier ?? "missing"} effective=${body.effective_tier ?? "missing"}`;
        }, fixture.org_id),
      { intervals: [2_000], timeout: 60_000 },
    )
    .toBe("tier=team effective=team");

  await page.reload();
  await unlockCurrentPage(page);
  await selectOwnerOrganisation(page);
  await expect(page.getByText(/Plan:\s*team/)).toBeVisible();
});
