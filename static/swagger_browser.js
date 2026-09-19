const SWAGGER_BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
const SWAGGER_DOCS_URL = `${SWAGGER_BASE}/docs`;
const OPENAPI_JSON_URL = `${SWAGGER_BASE}/openapi.json`;

function injectSwaggerBrowserStyles() {
  if (document.getElementById("swagger-browser-styles")) return;
  const style = document.createElement("style");
  style.id = "swagger-browser-styles";
  style.textContent = `
    .swagger-browser-launch { white-space: nowrap; }
    .swagger-browser-backdrop {
      position: fixed;
      inset: 0;
      z-index: 10990;
      background: var(--backdrop, rgba(20, 26, 35, .28));
      opacity: 0;
      pointer-events: none;
      transition: opacity .16s ease;
    }
    .swagger-browser-backdrop.open {
      opacity: 1;
      pointer-events: auto;
    }
    .swagger-browser-drawer {
      position: fixed;
      z-index: 11000;
      top: 12px;
      right: 12px;
      bottom: 12px;
      width: min(1100px, calc(100vw - 24px));
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--line, #d8dee7);
      border-radius: var(--radius, 8px);
      background: var(--surface, #fff);
      box-shadow: var(--shadow-md, 0 16px 40px rgba(16,24,40,.10));
      overflow: hidden;
      transform: translateX(calc(100% + 24px));
      transition: transform .18s ease;
    }
    .swagger-browser-drawer.open { transform: translateX(0); }
    .swagger-browser-head {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 58px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line, #d8dee7);
      background: var(--surface, #fff);
    }
    .swagger-browser-heading {
      min-width: 0;
      flex: 1;
      display: grid;
      gap: 2px;
    }
    .swagger-browser-heading strong { font-size: 15px; }
    .swagger-browser-heading small {
      color: var(--muted, #657285);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .swagger-browser-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .swagger-browser-actions a {
      text-decoration: none;
      color: inherit;
    }
    .swagger-browser-frame {
      width: 100%;
      height: 100%;
      border: 0;
      background: #fff;
    }
    @media (max-width: 640px) {
      .swagger-browser-drawer {
        inset: 0;
        width: 100vw;
        height: 100dvh;
        border: 0;
        border-radius: 0;
      }
      .swagger-browser-head {
        align-items: flex-start;
        flex-wrap: wrap;
      }
      .swagger-browser-heading {
        flex-basis: calc(100% - 48px);
      }
      .swagger-browser-actions {
        width: 100%;
        justify-content: flex-start;
      }
    }
  `;
  document.head.appendChild(style);
}

function installSwaggerBrowser() {
  const controls = document.querySelector(".controls");
  if (!controls || document.getElementById("swagger-browser-launch")) return;

  injectSwaggerBrowserStyles();

  const launch = document.createElement("button");
  launch.id = "swagger-browser-launch";
  launch.type = "button";
  launch.className = "ghost-button swagger-browser-launch";
  launch.textContent = "API";
  launch.title = "Browse the live Swagger / OpenAPI documentation";
  launch.setAttribute("aria-haspopup", "dialog");
  launch.setAttribute("aria-controls", "swagger-browser-drawer");
  launch.setAttribute("aria-expanded", "false");

  const themeToggle = document.getElementById("theme-toggle");
  controls.insertBefore(launch, themeToggle || controls.firstChild);

  const backdrop = document.createElement("div");
  backdrop.id = "swagger-browser-backdrop";
  backdrop.className = "swagger-browser-backdrop";

  const drawer = document.createElement("section");
  drawer.id = "swagger-browser-drawer";
  drawer.className = "swagger-browser-drawer";
  drawer.setAttribute("role", "dialog");
  drawer.setAttribute("aria-modal", "true");
  drawer.setAttribute("aria-label", "Swagger API browser");
  drawer.setAttribute("aria-hidden", "true");
  drawer.inert = true;

  drawer.innerHTML = `
    <div class="swagger-browser-head">
      <div class="swagger-browser-heading">
        <strong>Swagger API Browser</strong>
        <small>${SWAGGER_DOCS_URL}</small>
      </div>
      <div class="swagger-browser-actions">
        <a id="swagger-openapi-json" class="ghost-button" href="${OPENAPI_JSON_URL}" target="_blank" rel="noopener noreferrer">OpenAPI JSON</a>
        <a id="swagger-open-tab" class="ghost-button" href="${SWAGGER_DOCS_URL}" target="_blank" rel="noopener noreferrer">Open tab</a>
        <button id="swagger-reload" class="ghost-button" type="button">Reload</button>
        <button id="swagger-browser-close" class="icon-button" type="button" title="Close API browser" aria-label="Close API browser">×</button>
      </div>
    </div>
    <iframe
      id="swagger-browser-frame"
      class="swagger-browser-frame"
      title="Swagger API documentation"
      src="about:blank"
      data-src="${SWAGGER_DOCS_URL}"
    ></iframe>
  `;

  document.body.append(backdrop, drawer);

  const frame = drawer.querySelector("#swagger-browser-frame");
  const closeButton = drawer.querySelector("#swagger-browser-close");
  const reloadButton = drawer.querySelector("#swagger-reload");

  const ensureLoaded = () => {
    if (!frame.getAttribute("src") || frame.getAttribute("src") === "about:blank") {
      frame.setAttribute("src", frame.dataset.src);
    }
  };

  const open = () => {
    ensureLoaded();
    backdrop.classList.add("open");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    drawer.inert = false;
    launch.setAttribute("aria-expanded", "true");
    closeButton.focus();
  };

  const close = () => {
    backdrop.classList.remove("open");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawer.inert = true;
    launch.setAttribute("aria-expanded", "false");
    launch.focus();
  };

  launch.addEventListener("click", open);
  closeButton.addEventListener("click", close);
  backdrop.addEventListener("click", close);
  reloadButton.addEventListener("click", () => {
    const url = frame.dataset.src;
    frame.setAttribute("src", "about:blank");
    requestAnimationFrame(() => frame.setAttribute("src", url));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer.classList.contains("open")) {
      event.preventDefault();
      close();
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", installSwaggerBrowser, { once: true });
} else {
  installSwaggerBrowser();
}
