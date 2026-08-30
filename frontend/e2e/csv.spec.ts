/**
 * Downloads: the server-side CSV export (?format=csv, all rows regardless of paging)
 * and the sample FASTA file.
 */
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { GOLDEN, SAMPLE, keyOf, loadSampleAndRun } from "./helpers";

const CSV_HEADER = "transcript_id,position,mod_type,probability,p_value,coverage,source";

function nonEmptyLines(text: string): string[] {
  return text.split(/\r?\n/).filter((line) => line.length > 0);
}

test("Download CSV returns the server CSV with the header and all 22 rows", async ({ page }, testInfo) => {
  await loadSampleAndRun(page);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("download-csv").click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.csv$/i);
  const file = testInfo.outputPath("sites.csv");
  await download.saveAs(file);
  const lines = nonEmptyLines(readFileSync(file, "utf8"));

  expect(lines[0]).toBe(CSV_HEADER);
  expect(lines).toHaveLength(1 + GOLDEN.results.length); // header + 22 rows
  expect(lines.some((line) => /^[^,]*,52,Gm,/.test(line)), "row for 52,Gm present").toBe(true);

  const keys = lines.slice(1).map((line) => {
    const cells = line.split(",");
    expect(cells).toHaveLength(7);
    expect(cells[6]).toBe("sequence");
    return `${cells[1]}:${cells[2]}`;
  });
  expect(new Set(keys)).toEqual(new Set(GOLDEN.results.map(keyOf)));
});

test("Download sample saves a single-record FASTA of the 151-nt sample", async ({ page }, testInfo) => {
  await page.goto("/");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("download-sample").click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.fasta$/);
  const file = testInfo.outputPath("sample.fasta");
  await download.saveAs(file);
  const lines = nonEmptyLines(readFileSync(file, "utf8"));

  expect(lines[0]).toMatch(/^>\S/);
  expect(lines.filter((line) => line.startsWith(">"))).toHaveLength(1);
  expect(lines.slice(1).join("")).toBe(SAMPLE.sequence);
  expect(SAMPLE.sequence).toHaveLength(151);
});
