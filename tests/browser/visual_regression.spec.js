const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const root = path.resolve(__dirname, "..", "..");
const manifest = JSON.parse(
  fs.readFileSync(path.join(root, "docs", "screenshots", "manifest.json"), "utf8"),
);
const baselines = JSON.parse(
  fs.readFileSync(path.join(__dirname, "visual_baselines.json"), "utf8"),
);

async function visualSignature(page, capture) {
  await page.setViewportSize(capture.viewport || { width: 1440, height: 1000 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.stack || error.message));
  await page.goto(`http://127.0.0.1:18766/${capture.fixture}`, {
    waitUntil: "networkidle",
  });
  if (capture.open_event) {
    await page.evaluate((eventName) => {
      window.dispatchEvent(new CustomEvent(eventName));
    }, capture.open_event);
    await page.waitForTimeout(50);
  } else if (capture.open_selector) {
    const trigger = page.locator(capture.open_selector);
    await expect(trigger).toBeVisible();
    await trigger.click();
    await page.waitForTimeout(50);
  }
  expect(errors, `${capture.name}: uncaught page exceptions`).toEqual([]);

  const png = await page.screenshot({
    fullPage: true,
    animations: "disabled",
  });
  const encoded = png.toString("base64");
  return page.evaluate(async ({ encoded, grid }) => {
    const image = new Image();
    image.src = `data:image/png;base64,${encoded}`;
    await image.decode();

    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(
      0,
      0,
      image.naturalWidth,
      image.naturalHeight,
    ).data;

    const luma = [];
    for (let gy = 0; gy < grid; gy += 1) {
      const y0 = Math.floor((gy * image.naturalHeight) / grid);
      const y1 = Math.floor(((gy + 1) * image.naturalHeight) / grid);
      for (let gx = 0; gx < grid; gx += 1) {
        const x0 = Math.floor((gx * image.naturalWidth) / grid);
        const x1 = Math.floor(((gx + 1) * image.naturalWidth) / grid);
        let weighted = 0;
        let count = 0;
        for (let y = y0; y < y1; y += 1) {
          for (let x = x0; x < x1; x += 1) {
            const offset = ((y * image.naturalWidth) + x) * 4;
            weighted += (
              (299 * pixels[offset])
              + (587 * pixels[offset + 1])
              + (114 * pixels[offset + 2])
            );
            count += 1000;
          }
        }
        luma.push(Math.round(weighted / count));
      }
    }
    return {
      width: image.naturalWidth,
      height: image.naturalHeight,
      grid,
      luma,
    };
  }, { encoded, grid: baselines.captures[capture.name].grid });
}

for (const capture of manifest.captures) {
  test(`visual baseline: ${capture.name}`, async ({ page }) => {
    const expected = baselines.captures[capture.name];
    expect(expected, `${capture.name}: missing visual baseline`).toBeTruthy();

    const actual = await visualSignature(page, capture);
    expect(actual.width).toBe(expected.width);
    expect(
      Math.abs(actual.height - expected.height),
      `${capture.name}: full-page height drift`,
    ).toBeLessThanOrEqual(4);

    const deltas = actual.luma.map((value, index) => (
      Math.abs(value - expected.luma[index])
    ));
    const meanDelta = deltas.reduce((sum, value) => sum + value, 0) / deltas.length;
    const changedCells = deltas.filter((value) => value > 12).length;

    expect(
      meanDelta,
      `${capture.name}: visual signature mean luma drift; changed cells=${changedCells}`,
    ).toBeLessThanOrEqual(4);
    expect(
      changedCells,
      `${capture.name}: too many materially changed visual regions`,
    ).toBeLessThanOrEqual(4);
  });
}

test("visual baseline catalog exactly covers deterministic screenshot manifest", () => {
  expect(Object.keys(baselines.captures).sort()).toEqual(
    manifest.captures.map((capture) => capture.name).sort(),
  );
});
