import { confirmDiscard } from "./dirty_editor.js";

export function dialogShell(titleText, detail) {
  const dialog = document.createElement("dialog");
  dialog.className = "product-section-dialog";
  dialog.innerHTML = `
    <div class="product-section-dialog-shell">
      <header><div><h2></h2><p></p></div><button type="button" class="icon-button" data-close aria-label="Close">×</button></header>
      <div class="route-test" data-body></div>
      <div class="form-result" data-status hidden aria-live="polite"></div>
    </div>`;
  dialog.querySelector("h2").textContent = titleText;
  dialog.querySelector("header p").textContent = detail || "";
  dialog.querySelector("[data-close]").addEventListener("click", () => {
    if (!dialog.dirtyEditor || confirmDiscard(dialog.dirtyEditor)) dialog.close();
  });
  dialog.addEventListener('cancel', event => {
    if (dialog.dirtyEditor && !confirmDiscard(dialog.dirtyEditor)) event.preventDefault();
  });
  document.body.appendChild(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  return dialog;
}

export function status(dialog, message, failed = false) {
  const host = dialog.querySelector("[data-status]");
  host.hidden = false;
  host.textContent = message;
  host.classList.toggle("workspace-state-error", failed);
}

