import { esc } from './definition_typed_editor_shared.js';
import { trackDirtyEditor } from './dirty_editor.js';

export function procedureStartForm(refs = []) {
  return `<form data-procedure-start-form><label>Admitted exact revision<select name="definition">${refs.map((ref,i) => `<option value="${i}">${esc(ref.definition_id)} r${ref.revision} · ${esc(ref.checksum)}</option>`).join('')}</select></label><label>Governed input Artifact ID<input name="input_snapshot_id"></label><button type="submit">Start pinned playbook</button></form>`;
}

export function installCurrentInputWitness(host) {
  const fields = document.createElement('section');
  fields.innerHTML = '<label>Fresh equal-content input Artifact ID (optional)<input maxlength="200" data-current-input-witness></label><p>A witness permits current use of the same original input. It cannot change the run, scope or business facts.</p>';
  host.querySelector('[data-procedure-start-form]').after(fields);
  const editor = trackDirtyEditor(fields, {label:'Current input witness'});
  return {editor, fields, value:() => fields.querySelector('input').value.trim()};
}
