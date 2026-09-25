/**
 * #447 — a dialog whose code cannot be fetched, in a real browser.
 *
 * The realistic cause: a deploy replaces the content-hashed chunks while a
 * reader still holds the old index.html, so the Sign in dialog's chunk 404s.
 * Before #447 that rejected import unmounted the whole React root — the same
 * white screen #358 reproduced for the strip — and took the reader's on-screen
 * transcript with it.
 *
 * The unit tests (src/components/*.chunkFailure.test.tsx) mock the module. This
 * file answers the request with a real 404 and lets the browser's own module
 * loader reject, which is the part a mock cannot stand in for.
 *
 * The route matches the dev server's `/src/components/AuthModal.tsx` and a
 * production build's `/assets/AuthModal-<hash>.js`, so it bites under either.
 */
import { test, expect } from "./fixtures";
import { enterChat, gotoApp, waitStreamDone } from "./helpers";

const AUTH_MODAL_CHUNK = /\/AuthModal(\.tsx|-[\w-]+\.js)(\?|$)/;
const MESSAGE = "Part of the page didn't load. Reload the page and try again.";

test.describe("a dialog chunk that never arrives (#447)", () => {
  test("Sign in keeps the page, says to reload, and still works on a second click", async ({ page }) => {
    const hits: string[] = [];
    await page.route(AUTH_MODAL_CHUNK, (route) => {
      hits.push(route.request().url());
      return route.fulfill({ status: 404, body: "" });
    });
    await gotoApp(page);

    const signIn = page.locator(".account-button", { hasText: "Sign in" });
    await signIn.click();

    const toast = page.getByRole("alert").filter({ hasText: MESSAGE });
    await expect(toast).toBeVisible();
    // Partner: the 404 really was served. Without this, a route that matched
    // nothing would leave the dialog loading normally and the assertions
    // below would be about a page that never saw a failure.
    expect(hits.length).toBeGreaterThan(0);
    await expect(page.locator('[role="dialog"]')).toHaveCount(0);

    // Every one of these is zero on a white screen.
    await expect(page.locator("#demo")).toBeAttached();
    await expect(page.locator(".theme-toggle")).toBeAttached();
    expect(await page.evaluate(() => document.body.innerText.length)).toBeGreaterThan(500);

    // The button is not left dead: a second click is contained the same way.
    await toast.getByRole("button").click();
    await expect(toast).toHaveCount(0);
    await signIn.click();
    await expect(page.getByRole("alert").filter({ hasText: MESSAGE })).toBeVisible();
    await expect(page.locator("#demo")).toBeAttached();
  });

  test("Sign in from the chat screen does not lose the transcript", async ({ page }) => {
    await page.route(AUTH_MODAL_CHUNK, (route) => route.fulfill({ status: 404, body: "" }));
    await gotoApp(page);
    await enterChat(page);
    await page.locator(".chat-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);

    const question = page.locator(".message-body", { hasText: "What is Claude Code?" });
    await expect(question).toBeVisible();

    await page.locator(".account-button", { hasText: "Sign in" }).click();
    await expect(page.getByRole("alert").filter({ hasText: MESSAGE })).toBeVisible();

    await expect(question).toBeVisible();
    await expect(page.locator(".chat-input")).toBeVisible();
  });
});
