import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

// One transient form over the shared dirty-editor contract; no draft persistence.
export function formDraft(label) {
  let editor = null, root = null, generation = 0;
  const advance = () => { generation += 1; };
  for (const event of ['popstate', 'hashchange', 'codex:project-workspace-page', 'codex:project-changed']) window.addEventListener(event, advance);
  const clear = () => { editor?.dispose(); editor = null; root = null; advance(); };
  const view = () => {
    const version = generation, project = captureProjectView(), origin = location.href;
    return () => version === generation && project.current() && origin === location.href;
  };
  return {
    dirty: () => Boolean(editor?.dirty()),
    view, clear,
    mount(form) {
      editor?.dispose(); advance(); root = form;
      editor = trackDirtyEditor(form, { label });
    },
    leave() {
      if (editor && !confirmDiscard(editor)) return false;
      clear(); return true;
    },
    submission() {
      const selected = editor, form = root, snapshot = editor?.snapshot(), active = view();
      const current = () => active() && selected === editor && Boolean(form?.isConnected);
      return { current, saved() { if (current()) selected.markSaved(snapshot); } };
    },
  };
}
