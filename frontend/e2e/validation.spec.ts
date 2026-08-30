/**
 * Input validation: client-side messages before submit (`local-error`, Run disabled),
 * the server-side 422 path (`error` banner in plain English), the empty-result state,
 * and Clear.
 */
import { expect, test } from "@playwright/test";
import { GOLDEN_51NT, SAMPLE, fillSequence, loadSampleAndRun } from "./helpers";

const REPEAT_51 = "ACGT".repeat(13); // 52 nt, all valid

test.describe("client-side validation (before submit)", () => {
  test("50 nt: too short, Run disabled", async ({ page }) => {
    await page.goto("/");
    const seq = "ACGT".repeat(12) + "AC";
    expect(seq).toHaveLength(50);
    await fillSequence(page, seq);
    await expect(page.getByTestId("sequence-length")).toContainText("50 nt");
    const err = page.getByTestId("local-error");
    await expect(err).toBeVisible();
    await expect(err).toContainText("51");
    await expect(page.getByTestId("run")).toBeDisabled();
    await expect(page.getByTestId("error")).toHaveCount(0);
  });

  test("invalid character N is named in the message, Run disabled", async ({ page }) => {
    await page.goto("/");
    await fillSequence(page, REPEAT_51 + "N");
    const err = page.getByTestId("local-error");
    await expect(err).toBeVisible();
    await expect(err).toContainText("'N'");
    await expect(page.getByTestId("run")).toBeDisabled();
  });

  test("more than 10,000 nt: too long, Run disabled", async ({ page }) => {
    await page.goto("/");
    await fillSequence(page, "ACGT".repeat(2501)); // 10,004 nt
    const err = page.getByTestId("local-error");
    await expect(err).toBeVisible();
    await expect(err).toContainText("10,000");
    await expect(page.getByTestId("run")).toBeDisabled();
  });
});

test("51 nt: scored with zero sites -> empty state", async ({ page }) => {
  await page.goto("/");
  const seq = SAMPLE.sequence.slice(0, 51);
  await fillSequence(page, seq);
  await expect(page.getByTestId("sequence-length")).toContainText("51 nt");
  await expect(page.getByTestId("local-error")).toHaveCount(0);
  await page.getByTestId("run").click();
  await expect(page.getByTestId("results")).toBeVisible();
  await expect(page.getByTestId("empty")).toBeVisible();
  expect(GOLDEN_51NT.meta.n_sites).toBe(0);
  await expect(page.getByTestId("n-sites")).toHaveText("0");
  await expect(page.getByTestId("results-table")).toHaveCount(0);
  await expect(page.getByTestId("track-view")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test.describe("server-side 422 -> error banner", () => {
  test("pasting two FASTA records tells the user only one sequence is supported", async ({ page }) => {
    // The backend answers 422 "only one sequence per request is supported". The UI may
    // either forward that message (`error` banner) or say the same thing before submit
    // (`local-error`); what matters is that the user is not shown a misleading message
    // (e.g. a complaint about the '>' character) or raw JSON.
    await page.goto("/");
    await fillSequence(page, `>a\n${REPEAT_51}\n>b\n${REPEAT_51}\n`);
    const run = page.getByTestId("run");
    if (await run.isEnabled()) await run.click();

    const message = page.getByTestId("local-error").or(page.getByTestId("error"));
    await expect(message).toBeVisible();
    await expect(message).toContainText(/only one sequence|one sequence per request|single sequence|one (fasta )?record/i);
    await expect(message).not.toContainText("{");
    await expect(message).not.toContainText('"detail"');
    await expect(page.getByTestId("results")).toHaveCount(0);
    await expect(page.getByTestId("loading")).toHaveCount(0);
  });

  test("the `detail` of a 422 response is shown verbatim as text (stubbed response)", async ({ page }) => {
    const detail = "only one sequence per request is supported";
    await page.route("**/api/predict/sequence**", (route) =>
      route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail }),
      }),
    );
    await page.goto("/");
    await fillSequence(page, REPEAT_51);
    await page.getByTestId("run").click();
    const banner = page.getByTestId("error");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(detail);
    await expect(banner).not.toContainText("{");
    await expect(page.getByTestId("results")).toHaveCount(0);
    // The form is usable again.
    await expect(page.getByTestId("run")).toBeEnabled();
  });
});

test("Clear empties the textarea and removes the results", async ({ page }) => {
  await loadSampleAndRun(page);
  await expect(page.getByTestId("clear")).toBeEnabled();
  await page.getByTestId("clear").click();
  await expect(page.getByTestId("sequence-input")).toHaveValue("");
  await expect(page.getByTestId("sequence-length")).not.toContainText("nt");
  await expect(page.getByTestId("results")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
  await expect(page.getByTestId("local-error")).toHaveCount(0);
  await expect(page.getByTestId("run")).toBeDisabled();
  await expect(page.getByTestId("clear")).toBeDisabled();
});
