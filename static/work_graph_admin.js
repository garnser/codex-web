(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

  let actor = null;
  let projects = [];
  let graph = null;
  let events = [];
  let zoom = 1;
  let focusRef = null;
  let focusRefs = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function setStatus(message) {
    const host = document.getElementById("work-graph-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function setManagementStatus(message) {
    const host = document.getElementById("work-graph-management-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function canMutate() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("work-graph:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function renderAssurance() {
    const host = document.getElementById("work-graph-management-assurance");
    if (host && actor) {
      host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Graph mutation: ${canMutate() ? "allowed" : "blocked"}. Readiness remains deterministic and graph-runnable does not grant execution authority.`;
    }
    const add = document.getElementById("add-work-graph-edge");
    if (add) add.disabled = !canMutate();
  }

  function selectedProjectId() {
    return document.getElementById("work-graph-project")?.value || "";
  }

  function workItemUrl(ref) {
    const path = String(ref).split("/").map(encodeURIComponent).join("/");
    return `${BASE}/api/work-items/${path}/operator`;
  }

  function currentNodes() {
    if (!graph) return [];
    const search = (document.getElementById("work-graph-search")?.value || "").trim().toLowerCase();
    const readiness = document.getElementById("work-graph-readiness-filter")?.value || "";
    return graph.nodes.filter((node) => {
      if (readiness && node.readiness?.status !== readiness) return false;
      if (focusRefs && !focusRefs.has(node.ref)) return false;
      if (!search) return true;
      return [node.ref, node.title, node.stage, node.terminal_outcome]
        .filter(Boolean).join(" ").toLowerCase().includes(search);
    });
  }

  function visibleEdges(nodes) {
    if (!graph) return [];
    const refs = new Set(nodes.map((node) => node.ref));
    return graph.edges.filter((edge) => refs.has(edge.source_ref) && refs.has(edge.target_ref));
  }

  function layout(nodes, edges) {
    const refs = new Set(nodes.map((node) => node.ref));
    const incoming = new Map(nodes.map((node) => [node.ref, 0]));
    const outgoing = new Map(nodes.map((node) => [node.ref, []]));
    for (const edge of edges) {
      if (!refs.has(edge.source_ref) || !refs.has(edge.target_ref)) continue;
      incoming.set(edge.target_ref, (incoming.get(edge.target_ref) || 0) + 1);
      outgoing.get(edge.source_ref)?.push(edge.target_ref);
    }

    const queue = [...nodes.map((node) => node.ref).filter((ref) => incoming.get(ref) === 0)].sort();
    const depth = new Map(nodes.map((node) => [node.ref, 0]));
    while (queue.length) {
      const current = queue.shift();
      const children = [...(outgoing.get(current) || [])].sort();
      for (const child of children) {
        depth.set(child, Math.max(depth.get(child) || 0, (depth.get(current) || 0) + 1));
        incoming.set(child, (incoming.get(child) || 0) - 1);
        if (incoming.get(child) === 0) queue.push(child);
      }
      queue.sort();
    }

    const groups = new Map();
    for (const node of nodes) {
      const layer = depth.get(node.ref) || 0;
      if (!groups.has(layer)) groups.set(layer, []);
      groups.get(layer).push(node);
    }
    for (const group of groups.values()) {
      group.sort((left, right) => left.ref.localeCompare(right.ref));
    }

    const positions = new Map();
    let maxRows = 1;
    for (const [layer, group] of groups.entries()) {
      maxRows = Math.max(maxRows, group.length);
      group.forEach((node, row) => {
        positions.set(node.ref, {
          x: 30 + layer * 240,
          y: 30 + row * 112,
        });
      });
    }
    const maxLayer = Math.max(0, ...groups.keys());
    return {
      positions,
      width: Math.max(520, 240 * (maxLayer + 1) + 70),
      height: Math.max(220, maxRows * 112 + 60),
    };
  }

  function criticalEdgeSet() {
    const refs = graph?.critical_path?.refs || [];
    const result = new Set();
    for (let index = 0; index + 1 < refs.length; index += 1) {
      result.add(`${refs[index]}→${refs[index + 1]}`);
    }
    return result;
  }

  function renderSvg() {
    const svg = document.getElementById("work-graph-svg");
    if (!svg) return;
    const nodes = currentNodes();
    const edges = visibleEdges(nodes);
    if (!nodes.length) {
      svg.setAttribute("viewBox", "0 0 520 220");
      svg.innerHTML = '<text x="24" y="48" class="work-graph-empty">No nodes match the current graph filters.</text>';
      return;
    }

    const { positions, width, height } = layout(nodes, edges);
    svg.setAttribute("viewBox", `0 0 ${Math.ceil(width / zoom)} ${Math.ceil(height / zoom)}`);
    const criticalNodes = new Set(graph?.critical_path?.refs || []);
    const criticalEdges = criticalEdgeSet();

    const edgeMarkup = edges.map((edge) => {
      const source = positions.get(edge.source_ref);
      const target = positions.get(edge.target_ref);
      if (!source || !target) return "";
      const x1 = source.x + 180;
      const y1 = source.y + 34;
      const x2 = target.x;
      const y2 = target.y + 34;
      const critical = edge.relation === "blocks"
        && criticalEdges.has(`${edge.source_ref}→${edge.target_ref}`);
      const classes = [
        "work-graph-edge",
        `relation-${edge.relation}`,
        critical ? "critical" : "",
      ].filter(Boolean).join(" ");
      return `<path class="${classes}" d="M ${x1} ${y1} C ${x1 + 42} ${y1}, ${x2 - 42} ${y2}, ${x2} ${y2}" marker-end="url(#work-graph-arrow)"><title>${escapeHtml(edge.source_ref)} ${escapeHtml(edge.relation)} ${escapeHtml(edge.target_ref)}</title></path>`;
    }).join("");

    const nodeMarkup = nodes.map((node) => {
      const position = positions.get(node.ref);
      const status = node.readiness?.status || "unknown";
      const isCritical = criticalNodes.has(node.ref);
      const isFocused = focusRef === node.ref;
      const classes = [
        "work-graph-node",
        `status-${status}`,
        isCritical ? "critical" : "",
        isFocused ? "focused" : "",
      ].filter(Boolean).join(" ");
      const title = node.title || node.ref;
      const shortTitle = title.length > 25 ? `${title.slice(0, 24)}…` : title;
      return `<g class="${classes}" data-work-graph-node="${escapeHtml(node.ref)}" tabindex="0" role="button" aria-label="${escapeHtml(node.ref)} ${escapeHtml(status)}">
        <rect x="${position.x}" y="${position.y}" width="180" height="68" rx="8"></rect>
        <text x="${position.x + 10}" y="${position.y + 22}" class="work-graph-node-title">${escapeHtml(shortTitle)}</text>
        <text x="${position.x + 10}" y="${position.y + 42}" class="work-graph-node-ref">${escapeHtml(node.ref)}</text>
        <text x="${position.x + 10}" y="${position.y + 58}" class="work-graph-node-state">${escapeHtml(status)} · ${escapeHtml(node.stage)}</text>
      </g>`;
    }).join("");

    svg.innerHTML = `<defs>
      <marker id="work-graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z"></path>
      </marker>
    </defs>${edgeMarkup}${nodeMarkup}`;
  }

  function renderProgress() {
    const host = document.getElementById("work-graph-progress");
    if (!host) return;
    if (!graph) {
      host.innerHTML = "";
      return;
    }
    const progress = graph.progress || {};
    host.innerHTML = `<div class="comm-entry">
      <strong>Project graph progress · ${Math.round(Number(progress.completion_fraction || 0) * 100)}%</strong>
      <small>Total ${progress.total || 0} · completed ${progress.completed || 0} · active ${progress.active || 0} · runnable ${progress.runnable || 0} · blocked ${progress.blocked || 0} · failed ${progress.failed || 0} · cancelled ${progress.cancelled || 0}</small>
      <small>Parallel runnable Work Items: ${(graph.runnable_refs || []).map(escapeHtml).join(", ") || "none"}.</small>
      <small>Critical path: ${(graph.critical_path?.refs || []).map(escapeHtml).join(" → ") || "none"}.</small>
    </div>` + (graph.failure_impacts || []).map((impact) => `<div class="comm-entry">
      <strong>Dependency failure impact · ${escapeHtml(impact.behavior)}</strong>
      <small>${escapeHtml(impact.blocker_ref)} → ${escapeHtml(impact.blocked_ref)} · blocker outcome ${escapeHtml(impact.blocker_outcome)}</small>
      <small>${escapeHtml(impact.reason)}</small>
    </div>`).join("");
  }

  function renderNodeDetail(ref) {
    const host = document.getElementById("work-graph-node-detail");
    if (!host) return;
    const node = graph?.nodes?.find((item) => item.ref === ref);
    if (!node) {
      host.innerHTML = '<div class="comm-entry"><strong>Select a graph node to inspect deterministic readiness.</strong></div>';
      return;
    }
    const readiness = node.readiness || {};
    host.innerHTML = `<div class="comm-entry">
      <strong>${escapeHtml(node.title || node.ref)} · ${escapeHtml(readiness.status)}</strong>
      <small>Ref: ${escapeHtml(node.ref)} · stage: ${escapeHtml(node.stage)} · terminal outcome: ${escapeHtml(node.terminal_outcome || "none")}</small>
      <small>Why: ${(readiness.reasons || []).map(escapeHtml).join(" · ") || "No graph blocker; node is runnable subject to normal authority/policy/resource/worker/workspace gates."}</small>
      <small>Blocking refs: ${(readiness.blocking_refs || []).map(escapeHtml).join(", ") || "none"}.</small>
      <a href="${escapeHtml(workItemUrl(node.ref))}" target="_blank" rel="noopener noreferrer">Open canonical Work Item operator detail</a>
    </div>`;
  }

  function nodeOptions() {
    return (graph?.nodes || []).map((node) => (
      `<option value="${escapeHtml(node.ref)}">${escapeHtml(node.title || node.ref)} · ${escapeHtml(node.ref)}</option>`
    )).join("");
  }

  function renderManagement() {
    const edgeHost = document.getElementById("work-graph-edge-list");
    const source = document.getElementById("work-graph-source");
    const target = document.getElementById("work-graph-target");
    const options = nodeOptions();
    if (source) source.innerHTML = options;
    if (target) {
      target.innerHTML = options;
      if (target.options.length > 1) target.selectedIndex = 1;
    }
    if (!edgeHost) return;
    edgeHost.innerHTML = (graph?.edges || []).map((edge) => `<div class="comm-entry">
      <strong>${escapeHtml(edge.source_ref)} ${escapeHtml(edge.relation)} ${escapeHtml(edge.target_ref)}</strong>
      <small>Failure behavior: ${escapeHtml(edge.failure_behavior)} · created by ${escapeHtml(edge.created_by)} · ${timeText(edge.created_at)}</small>
      ${edge.reason ? `<small>Reason: ${escapeHtml(edge.reason)}</small>` : ""}
      ${canMutate() ? `<button type="button" class="ghost-button" data-work-graph-remove-edge="${escapeHtml(edge.id)}">Remove relationship</button>` : ""}
    </div>`).join("") || '<div class="comm-entry"><strong>No explicit graph relationships.</strong></div>';
  }

  function renderEvents() {
    const host = document.getElementById("work-graph-events");
    if (!host) return;
    host.innerHTML = events.map((event) => `<div class="comm-entry">
      <strong>${escapeHtml(event.event_type)} · ${timeText(event.occurred_at)}</strong>
      <small>${escapeHtml(event.edge.source_ref)} ${escapeHtml(event.edge.relation)} ${escapeHtml(event.edge.target_ref)} · actor ${escapeHtml(event.actor_id)}</small>
      <small>Edge: ${escapeHtml(event.edge.id)} · failure behavior ${escapeHtml(event.edge.failure_behavior)}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No graph audit events for this project.</strong></div>';
  }

  function renderAll() {
    renderAssurance();
    renderProgress();
    renderSvg();
    renderManagement();
    renderEvents();
    if (focusRef) renderNodeDetail(focusRef);
    else renderNodeDetail(null);
    const visible = currentNodes().length;
    setStatus(graph
      ? `${visible} of ${graph.nodes.length} Work Item node(s) visible · ${graph.edges.length} relationship(s). Readiness, failure impact and critical path are deterministic server state.`
      : "No graph loaded.");
  }

  async function focusNode(ref) {
    if (!graph) return;
    focusRef = ref;
    try {
      const params = new URLSearchParams({ ref });
      const [upstream, downstream] = await Promise.all([
        apiRequest(`/api/work-graph/traverse?${params.toString()}&direction=upstream`),
        apiRequest(`/api/work-graph/traverse?${params.toString()}&direction=downstream`),
      ]);
      focusRefs = new Set([ref, ...(upstream.refs || []), ...(downstream.refs || [])]);
    } catch (error) {
      focusRefs = new Set([ref]);
      setStatus(`Focused node, but traversal expansion failed: ${error.message}`);
    }
    renderAll();
  }

  async function loadGraph() {
    const projectId = selectedProjectId();
    if (!projectId) {
      graph = null;
      events = [];
      renderAll();
      return;
    }
    setStatus("Loading canonical work graph...");
    const [snapshot, eventResponse] = await Promise.all([
      apiRequest(`/api/work-graph/projects/${encodeURIComponent(projectId)}`),
      apiRequest(`/api/work-graph/events?project_id=${encodeURIComponent(projectId)}&limit=100`),
    ]);
    graph = snapshot.graph;
    events = eventResponse.items || [];
    focusRef = null;
    focusRefs = null;
    renderAll();
  }

  async function refresh() {
    try {
      const [projectResponse, me] = await Promise.all([
        apiRequest("/api/projects"),
        apiRequest("/api/identity/me"),
      ]);
      projects = Array.isArray(projectResponse) ? projectResponse : (projectResponse.items || []);
      actor = me;
      const select = document.getElementById("work-graph-project");
      if (select) {
        const current = select.value;
        select.innerHTML = projects.map((project) => (
          `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)} · ${escapeHtml(project.id)}</option>`
        )).join("");
        if (projects.some((project) => project.id === current)) select.value = current;
      }
      await loadGraph();
    } catch (error) {
      graph = null;
      events = [];
      setStatus(`Work Graph unavailable: ${error.message}`);
      renderAll();
    }
  }

  async function addEdge() {
    if (!canMutate()) return setManagementStatus("Graph mutation requires admin + MFA/step-up or work-graph:admin service authority.");
    const relation = document.getElementById("work-graph-relation")?.value || "blocks";
    const sourceRef = document.getElementById("work-graph-source")?.value || "";
    const targetRef = document.getElementById("work-graph-target")?.value || "";
    const failure = document.getElementById("work-graph-failure-behavior")?.value || "pause";
    const reason = document.getElementById("work-graph-edge-reason")?.value.trim() || null;
    if (!sourceRef || !targetRef) return setManagementStatus("Choose both source and target Work Items.");
    if (sourceRef === targetRef) return setManagementStatus("A relationship cannot target the same Work Item.");
    const failureBehavior = relation === "parent" ? "pause" : failure;
    if (!window.confirm(
      `Add ${relation} relationship ${sourceRef} → ${targetRef}? The server will reject cycles, scope conflicts and duplicate-policy conflicts before saving.`,
    )) return;
    try {
      await apiRequest("/api/work-graph/edges", {
        method: "POST",
        body: JSON.stringify({
          relation,
          source_ref: sourceRef,
          target_ref: targetRef,
          failure_behavior: failureBehavior,
          reason,
        }),
      });
      setManagementStatus("Relationship saved through the canonical graph service.");
      await loadGraph();
    } catch (error) {
      setManagementStatus(`Relationship rejected: ${error.message}`);
    }
  }

  async function removeEdge(edgeId) {
    const edge = graph?.edges?.find((item) => item.id === edgeId);
    if (!edge) return;
    if (!canMutate()) return setManagementStatus("Graph mutation requires elevated authority.");
    if (!window.confirm(
      `Remove ${edge.source_ref} ${edge.relation} ${edge.target_ref}? Readiness will be recomputed from the remaining canonical graph and Work Item state.`,
    )) return;
    try {
      await apiRequest(`/api/work-graph/edges/${encodeURIComponent(edgeId)}`, {
        method: "DELETE",
      });
      setManagementStatus("Relationship removed; graph readiness recomputed.");
      await loadGraph();
    } catch (error) {
      setManagementStatus(`Relationship removal failed: ${error.message}`);
    }
  }

  function syncRelationControls() {
    const relation = document.getElementById("work-graph-relation")?.value || "blocks";
    const failure = document.getElementById("work-graph-failure-behavior");
    if (failure) {
      failure.disabled = relation === "parent";
      if (relation === "parent") failure.value = "pause";
    }
  }

  function changeZoom(delta) {
    zoom = Math.min(2, Math.max(0.5, Number((zoom + delta).toFixed(2))));
    renderSvg();
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-work-graph")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("work-graph-project")?.addEventListener("change", () => loadGraph().catch(console.error));
    document.getElementById("work-graph-search")?.addEventListener("input", renderAll);
    document.getElementById("work-graph-readiness-filter")?.addEventListener("change", renderAll);
    document.getElementById("work-graph-relation")?.addEventListener("change", syncRelationControls);
    document.getElementById("add-work-graph-edge")?.addEventListener("click", () => addEdge().catch(console.error));
    document.getElementById("work-graph-zoom-in")?.addEventListener("click", () => changeZoom(0.15));
    document.getElementById("work-graph-zoom-out")?.addEventListener("click", () => changeZoom(-0.15));
    document.getElementById("work-graph-clear-focus")?.addEventListener("click", () => {
      focusRef = null;
      focusRefs = null;
      renderAll();
    });
    document.getElementById("work-graph-svg")?.addEventListener("click", (event) => {
      const node = event.target.closest?.("[data-work-graph-node]");
      if (node) focusNode(node.dataset.workGraphNode).catch(console.error);
    });
    document.getElementById("work-graph-svg")?.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      const node = event.target.closest?.("[data-work-graph-node]");
      if (!node) return;
      event.preventDefault();
      focusNode(node.dataset.workGraphNode).catch(console.error);
    });
    document.getElementById("work-graph-edge-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-work-graph-remove-edge]");
      if (button) removeEdge(button.dataset.workGraphRemoveEdge).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open && !graph) refresh().catch(console.error);
    });
    syncRelationControls();
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
