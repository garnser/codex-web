import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import http from "node:http";
import { chromium } from "playwright";

const root = path.resolve(import.meta.dirname, "..");
const manifestPath = path.join(root, "docs", "screenshots", "manifest.json");
const outputDir = path.join(root, "docs", "assets", "screenshots", "generated");
const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const port = Number(process.env.CODEX_WEB_DOC_SCREENSHOT_PORT || "18767");
const host = "127.0.0.1";
const base = `http://${host}:${port}`;

await fs.mkdir(outputDir, { recursive: true });

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", host], {
  cwd: root,
  stdio: ["ignore", "ignore", "inherit"],
});

function probe(url) {
  return new Promise((resolve) => {
    const request = http.get(url, (response) => {
      response.resume();
      response.once("end", () => {
        resolve(
          response.statusCode !== undefined
          && response.statusCode >= 200
          && response.statusCode < 400
        );
      });
    });
    request.once("error", () => resolve(false));
    request.setTimeout(1000, () => {
      request.destroy();
      resolve(false);
    });
  });
}

async function ready() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (await probe(`${base}/static/index.html`)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("documentation screenshot HTTP server did not become ready");
}

let browser;
try {
  await ready();
  browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  let currentCapture = null;
  const pageErrors = [];
  page.on("pageerror", (error) => {
    pageErrors.push({
      capture: currentCapture,
      message: error?.stack || error?.message || String(error),
    });
  });

  for (const capture of manifest.captures) {
    await page.setViewportSize(capture.viewport || { width: 1440, height: 1000 });
    currentCapture = capture.name;
    pageErrors.length = 0;
    const url = `${base}/${capture.fixture}`;
    await page.goto(url, { waitUntil: "networkidle" });
    if (capture.open_event) {
      await page.evaluate((eventName) => {
        window.dispatchEvent(new CustomEvent(eventName));
      }, capture.open_event);
      await page.waitForTimeout(50);
    } else if (capture.open_selector) {
      const trigger = page.locator(capture.open_selector);
      await trigger.waitFor({ state: "visible" });
      await trigger.click();
      await page.waitForTimeout(50);
    }
    if (pageErrors.length) {
      throw new Error(
        `${capture.name}: uncaught page exception(s):\n`
        + pageErrors.map((item) => item.message).join("\n---\n"),
      );
    }
    const text = await page.locator("body").innerText();
    for (const marker of manifest.forbidden_markers || []) {
      if (text.includes(marker)) {
        throw new Error(`${capture.name}: forbidden sensitive marker ${marker}`);
      }
    }
    for (const landmark of capture.landmarks || []) {
      if (!text.includes(landmark)) {
        throw new Error(`${capture.name}: missing required landmark ${landmark}`);
      }
    }
    await page.screenshot({
      path: path.join(outputDir, `${capture.name}.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  await fs.writeFile(
    path.join(outputDir, "manifest.json"),
    JSON.stringify(manifest, null, 2) + "\n",
    "utf8",
  );
  console.log(`Generated ${manifest.captures.length} sanitized documentation screenshots in ${outputDir}`);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
}
