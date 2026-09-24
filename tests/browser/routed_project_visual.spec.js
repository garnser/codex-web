const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const fixtureHtml = fs.readFileSync(
  path.join(__dirname, "product_workspaces_fixture.html"),
  "utf8",
);
const baselines = JSON.parse(
  fs.readFileSync(path.join(__dirname, "routed_project_visual_baselines.json"), "utf8"),
);

async function serveRoutedProjectShell(page) {
  await page.route("**/projects/**", async (route) => {
    if (route.request().resourceType() !== "document") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: fixtureHtml,
    });
  });
}

async function routedVisualSignature(page, capture) {
  await page.setViewportSize(capture.viewport);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.stack || error.message));
  await serveRoutedProjectShell(page);
  await page.goto(`http://127.0.0.1:18766${capture.path}`, {
    waitUntil: "load",
  });

  const surface = page.locator("#product-workspace-page");
  await expect(surface).toBeVisible();
  await expect(page.locator("dialog#product-workspace-dialog")).toHaveCount(0);
  await expect(page.locator(".main")).toBeHidden();
  await expect(page.locator("[data-product-workspace-title]")).toHaveText(capture.title);
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
  }, { encoded, grid: capture.grid });
}

for (const capture of baselines.captures) {
  test(`routed Project visual baseline: ${capture.name}`, async ({ page }) => {
    const actual = await routedVisualSignature(page, capture);
    const diagnostic = `${capture.name}: actual=${JSON.stringify(actual)}`;

    expect(actual.width, diagnostic).toBe(capture.width);
    expect(Math.abs(actual.height - capture.height), diagnostic).toBeLessThanOrEqual(4);

    const deltas = actual.luma.map((value, index) => (
      Math.abs(value - capture.luma[index])
    ));
    const meanDelta = deltas.reduce((sum, value) => sum + value, 0) / deltas.length;
    const changedCells = deltas.filter((value) => value > 12).length;

    expect(meanDelta, diagnostic).toBeLessThanOrEqual(4);
    expect(changedCells, diagnostic).toBeLessThanOrEqual(4);
  });
}
