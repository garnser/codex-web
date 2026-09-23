# UI migration inventory

This inventory tracks the remaining compatibility surfaces while the redesigned human-agent workspace replaces older page-local UI. It is intentionally conservative: a compatibility path stays until its supported behavior has an authoritative replacement and browser coverage.

| Area | Current compatibility surface | Replacement / target | Status | Removal condition |
| --- | --- | --- | --- | --- |
| Global styling | `static/styles.css` | `design_tokens.css`, `product_workspaces.css`, `workspace_components.css` and surface-local styles | compatibility | Remove rules only after every selector has a migrated consumer or equivalent replacement. |
| Sidebar Project list | `#projects` rendering in `static/app.js` | canonical Project switcher in the workspace shell | compatibility | Keep until Project creation/inspection and all deep-link flows are available from the replacement shell. |
| Sidebar Threads list | `#threads` rendering in `static/app.js` | Work/Thread workspace navigation | compatibility | Keep until thread search, creation, selection and active-turn state have full replacement coverage. |
| Developer cards | `.developer-card` surfaces | workspace-specific Operations/Organization surfaces | compatibility adapter | `product_workspaces.js` may adopt cards temporarily; remove each card only after the owning replacement surface has parity. |
| Work Items operator | `static/work_items_ui.js` dialog opened from the Work workspace | redesigned Work Item workspace and Run inspector | replacement active | Legacy top-bar launcher removed; retain the canonical operator module until its task-source configuration, retry/reconcile, and Run inspection functions have another authoritative consumer. |
| Agent provider/session admin | `static/agent_provider_admin.js` adopted by Operations | Operations workspace | compatibility adapter | Retain the canonical provider/runtime mutation and diagnostic controls while Operations hosts them; remove the adoption adapter only when an equivalent first-class Operations implementation owns the same actions. |
| Workspace component primitives | page-local badges/cards/state panels | `static/workspace_components.js` / `.css` | replacement active | Migrate consumers opportunistically; remove duplicate page-local implementations once no supported consumer remains. |

## Rules

- Do not remove a compatibility surface solely because a visually similar replacement exists.
- Canonical APIs, authorization, provenance, readiness and lifecycle semantics remain authoritative.
- Every removal should update this inventory in the same change.
- Browser regression coverage must remain green after each cleanup slice.
- Temporary backup/copy artifacts are never valid migration mechanisms and are rejected by tests.
