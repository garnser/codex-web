import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

let editor = null;
let generation = 0;

export function skillEditorOpen() { return Boolean(editor?.root.isConnected); }
export function leaveSkillEditor() {
  if (editor && !confirmDiscard(editor.dirty)) return false;
  editor?.dirty.dispose(); editor = null; generation += 1;
  return true;
}
export function skillView(root) {
  const visit = generation, project = captureProjectView();
  return { current: () => root.isConnected && generation === visit && project.current() };
}
export function trackSkillEditor(root, onDiscard) {
  const dirty = trackDirtyEditor(root, { label: 'Skill draft', onDiscard });
  editor = { root, dirty };
  return dirty;
}
export function trackSkillImport(root, label) {
  return trackDirtyEditor(root, { label });
}
