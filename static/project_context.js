const ACTIVE_PROJECT_KEY = "codex-web-active-project";

export function initialProjectId() {
  return new URLSearchParams(location.search).get("project")
    || sessionStorage.getItem(ACTIVE_PROJECT_KEY)
    || "home";
}

export function activateProject(state, projectId, { historyMode = "replace" } = {}) {
  const id = String(projectId || "").trim();
  if (!id) return "";
  state.projectId = id;
  sessionStorage.setItem(ACTIVE_PROJECT_KEY, id);
  if (document.body) document.body.dataset.projectId = id;
  const url = new URL(location.href);
  url.searchParams.set("project", id);
  const snapshot = { ...history.state, projectId: id };
  if (historyMode === "push") history.pushState(snapshot, "", url);
  else if (historyMode !== "none") history.replaceState(snapshot, "", url);
  dispatchEvent(new CustomEvent("codex:project-changed", { detail: { projectId: id } }));
  return id;
}

export function publishProjectContext(projects, projectId) {
  dispatchEvent(new CustomEvent("codex:projects-rendered", {
    detail: {
      projectId,
      projects: projects.map(({ id, name, path }) => ({ id, name, path })),
    },
  }));
}

export function installProjectNavigation(state, refreshSelection, onError = console.error) {
  const select = async (projectId, { historyMode = "push" } = {}) => {
    const id = String(projectId || "").trim();
    if (!id || id === state.projectId) return;
    activateProject(state, id, { historyMode });
    await refreshSelection();
  };
  const run = (projectId, options) => select(projectId, options).catch(onError);
  addEventListener("codex:project-select", event => run(event.detail?.projectId));
  addEventListener("popstate", () => {
    const id = new URLSearchParams(location.search).get("project");
    if (id && id !== state.projectId) run(id, { historyMode: "none" });
  });
  return select;
}

export function renderProjectList(c,ps,a,u){
  c.innerHTML="";
  for(const p of ps){
    const x=u.expanded(p.id),e=document.createElement("div");
    e.className=`item ${p.id===a?"active":""} ${x?"expanded":""}`;
    e.dataset.projectId=p.id;
    e.innerHTML=`<div class="item-header"><div class="item-main"><strong>${u.escape(p.name)}</strong><span>${u.escape(p.path)}</span></div><button class="item-expand-button" data-action="expand" aria-expanded="${x}">Actions</button></div><div class="item-actions" ${x?"":"hidden"}><button class="item-action-button" data-action="bot">Bot Integration</button></div>`;
    e.querySelector(".item-main").onclick=()=>u.select(p.id);
    e.querySelector('[data-action="expand"]').onclick=v=>{v.stopPropagation();u.toggle(p.id)};
    e.querySelector('[data-action="bot"]').onclick=v=>{v.stopPropagation();u.bot(p)};
    c.append(e);
  }
  publishProjectContext(ps,a);
}
