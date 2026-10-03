import { captureProjectView } from './project_view_scope.js';

let pending = false;
let sequence = 0;
const style = document.createElement('link');
style.rel = 'stylesheet'; style.href = new URL('./action_confirmation.css', import.meta.url).href;
document.head.appendChild(style);

// Shared keyboard/scope lifecycle for native modal action dialogs, including
// domain forms that retain their own canonical impact/reason controls.
export function protectActionDialog(dialog, { risk = 'bounded', current = () => true, trigger = document.activeElement } = {}) {
  const view = captureProjectView(); const origin = location.href;
  const valid = () => { try { return view.current() && origin === location.href && current(); } catch { return false; } };
  dialog.classList.add('action-confirmation'); dialog.dataset.actionConfirmation = risk;
  const heading = dialog.querySelector('h2');
  if (heading) { heading.id ||= `action-confirmation-${++sequence}-title`; dialog.setAttribute('aria-labelledby', heading.id); }
  const cancel = () => dialog.close('cancel');
  const events = ['popstate', 'hashchange', 'codex:project-changed', 'codex:project-workspace-page'];
  for (const event of events) window.addEventListener(event, cancel);
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...dialog.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')].filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  dialog.addEventListener('close', () => {
    for (const event of events) window.removeEventListener(event, cancel);
    setTimeout(() => { if (trigger?.isConnected && !trigger.disabled) trigger.focus(); }, 0);
  }, { once: true });
  return { current: valid };
}

// Presentation only. Domain APIs own authorization, dependency gates and CAS.
// Reversible low-impact work should use its normal action and supported recovery
// path directly. Callers must not invent Undo for an irreversible operation.
export function confirmAction({ action, target, consequence, risk = 'bounded', impact = '', recovery = '', current = () => true, trigger = document.activeElement }) {
  if (pending || !action || !target || !consequence || !['bounded', 'high'].includes(risk)) return Promise.resolve(false);
  try { if (!current()) return Promise.resolve(false); } catch { return Promise.resolve(false); }
  pending = true;
  const dialog = document.createElement('dialog'); dialog.className = 'action-confirmation'; dialog.dataset.actionConfirmation = risk;
  const id = `action-confirmation-${++sequence}`;
  dialog.setAttribute('aria-labelledby', `${id}-title`); dialog.setAttribute('aria-describedby', `${id}-effect`);
  dialog.innerHTML = `<h2></h2><p data-action-target></p><p data-action-effect></p><section data-action-impact></section><p data-action-recovery></p>
    <label data-action-ack-label><input type="checkbox" data-action-ack> I reviewed the target and consequences.</label>
    <div class="action-confirmation-buttons"><button type="button" data-action-cancel>Cancel</button><button type="button" data-action-apply></button></div>`;
  const title = dialog.querySelector('h2'); title.id = `${id}-title`; title.textContent = action;
  dialog.querySelector('[data-action-target]').textContent = `Target: ${target}`;
  const effect = dialog.querySelector('[data-action-effect]'); effect.id = `${id}-effect`; effect.textContent = consequence;
  const dependencies = dialog.querySelector('[data-action-impact]'); dependencies.textContent = impact; dependencies.hidden = !impact;
  const recoveryText = dialog.querySelector('[data-action-recovery]'); recoveryText.textContent = recovery; recoveryText.hidden = !recovery;
  const apply = dialog.querySelector('[data-action-apply]'); apply.textContent = action;
  apply.disabled = risk === 'high';
  dialog.querySelector('[data-action-ack-label]').hidden = risk !== 'high';
  dialog.querySelector('[data-action-ack]').onchange = event => { apply.disabled = !event.target.checked; };
  document.body.appendChild(dialog);
  const guard = protectActionDialog(dialog, { risk, current, trigger });
  return new Promise(resolve => {
    const cancel = () => dialog.close('cancel');
    dialog.querySelector('[data-action-cancel]').onclick = cancel;
    apply.onclick = () => { if (!apply.disabled) dialog.close(guard.current() ? 'apply' : 'cancel'); };
    dialog.addEventListener('close', () => {
      const accepted = dialog.returnValue === 'apply' && guard.current();
      dialog.remove(); pending = false; resolve(accepted);
    }, { once: true });
    dialog.showModal(); dialog.querySelector('[data-action-cancel]').focus();
  });
}
