// Read-only cache of the canonical assignment projection; never an authority
// check or a replacement for Thread/turn admission and fencing on the server.
import {esc} from './definition_typed_editor_shared.js';
const runners = new Map();
export function applyRunner(thread) {
  const next = thread?.runner;
  if (!thread?.id || !next || next.schemaVersion !== '1.0') return;
  const current = runners.get(thread.id);
  if (current && ((next.observedAt || 0) < (current.observedAt || 0)
      || (next.assignmentId && current.assignmentId && (next.assignmentCreatedAt || 0) < (current.assignmentCreatedAt || 0))
      || (next.assignmentId === current.assignmentId && (next.fence || 0) < (current.fence || 0))
      || (next.assignmentId === current.assignmentId && (next.updatedAt || 0) < (current.updatedAt || 0)))) return;
  // Retain ordering watermarks across an unavailable read. Unknown evidence
  // must not let a late snapshot of an older assignment regain custody.
  runners.set(thread.id, {...current, ...next, currentActivity: thread.activity});
  return true;
}
export function runnerFor(threadId) { return runners.get(threadId); }
export function allowsRuntimeActivity(threadId) {
  const runner = runners.get(threadId);
  return !runner || (runner.live && runner.leaseValid && runner.leaseExpiresAt > Date.now() / 1000);
}
export function runnerLabel(threadId) {
  const runner = runners.get(threadId);
  if (!runner) return '';
  if (runner.live && !allowsRuntimeActivity(threadId)) return 'Unknown runner · lease expired';
  const label = {running: 'Runner live', waiting: 'Waiting for worker', cancelled: 'Runner cancelled',
    idle: 'Runner idle', failed: 'Runner failed', lost: 'Runner lost · recovery required',
    recovering: 'Runner recovering · waiting for worker',
    stopped: runner.diagnosticState === 'unavailable' ? 'Runner stopped · diagnostic evidence unavailable' : 'Runner stopped · recovery required',
    unloaded: 'Runner unloaded', unknown: 'Unknown runner'}[runner.state] || 'Unknown runner';
  const validation = {captured:'validation report captured · outcome unverified', registered:'validation custody registered',
    unavailable:'validation evidence unavailable'}[runner.validationCustodyState];
  return label + (validation ? ` · ${validation}` : '');
}

// Presentation freshness only. Reuse the canonical projection; neither a live
// process nor retained turn history establishes current work. Two existing
// 30-second refresh intervals bound how long a missing observation can glow.
function freshUntil(runner) {
  return Math.min(runner.leaseExpiresAt || 0, (runner.observedAt || 0) + 60);
}
export function runnerPresentation(threadId, now = Date.now() / 1000) {
  const runner = runnerFor(threadId);
  if (!runner) return {state:'unknown', glyph:'?', label:'Unknown runner'};
  let state = runner.state;
  let label = runnerLabel(threadId);
  if (runner.live) {
    if (!runner.leaseValid || !Number.isFinite(freshUntil(runner)) || freshUntil(runner) <= now
        || runner.processRunning !== true || runner.state !== 'running') {
      return {state:'unknown', glyph:'?', label:label + ' · current custody unavailable'};
    }
    const activity = runner.currentActivity;
    state = activity?.source === 'current_turn' && activity.state === 'active' && activity.turnId
      ? 'active' : activity?.source === 'current_turn' && activity.state === 'idle' ? 'idle' : 'unknown';
    label += state === 'active' ? ' · active work' : state === 'idle' ? ' · idle' : ' · current activity unknown';
  }
  const glyph = {active:'●', idle:'○', waiting:'◷', recovering:'↻', stopped:'■',
    cancelled:'■', unloaded:'□', lost:'!', failed:'×', unknown:'?'};
  return {state: Object.hasOwn(glyph, state) ? state : 'unknown', glyph:glyph[state] || '?', label};
}

export function runnerBadgeHtml(threadId, {waiting = false, queued = 0} = {}) {
  const item = runnerPresentation(threadId);
  if (!runnerFor(threadId) && waiting) Object.assign(item, {state:'waiting', glyph:'◷', label:'Waiting for Codex · custody unavailable'});
  const badge = (state, glyph, label) => `<span class="thread-state runner-status runner-${state}" data-runner-state="${state}" title="Current canonical custody and activity; retained turns are history"><span aria-hidden="true">${glyph}</span>${esc(label)}</span>`;
  return badge(item.state, item.glyph, item.label)
    + (queued > 0 ? badge('queued', '≡', `Queued ${queued}`) : '');
}

// Safe, same-fence process observations do not establish business outcomes.
export function runnerFailureText(runner) {
  const failure = runner?.failureObservation;
  if (!failure || failure.assignment_id !== runner.assignmentId || failure.fence !== runner.fence) return '';
  const timings = Object.entries(failure.timings || {})
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .map(([phase, seconds]) => `${phase}: ${seconds.toFixed(3)}s`).join(', ');
  return `${failure.code} · phase ${failure.phase} · cleanup ${failure.cleanup_state}`
    + (timings ? ` · ${timings}` : '')
    + ` · diagnostic ${runner.diagnosticState} · unfinished work outcome unknown. Inspect assignment and evidence; reconcile provider receipts before recovery.`;
}

export function appendRunnerFailure(container, runner, projectId) {
  if (runner?.failureObservation?.project_id !== projectId) return;
  const text = runnerFailureText(runner);
  if (!text) return;
  const detail = document.createElement('span');
  detail.dataset.runnerFailure = '';
  detail.textContent = ` · ${text} `;
  container.append(detail);
  // Resolve shared navigation only in a browser; retain the observed Project
  // across the asynchronous module load and never borrow a newly selected one.
  if (!projectId) return;
  import('./reference_navigation.js').then(({referencePath, projectPath}) => {
    if (!detail.isConnected) return;
    for (const [kind, id] of [['assignment', runner.assignmentId], ['evidence', runner.failureObservation.evidence_id]]) {
      const path = referencePath(kind, id, {projectId});
      if (!path) continue;
      const link = document.createElement('a');
      link.href = path;
      link.dataset.managedReference = '';
      link.textContent = `View ${kind}`;
      detail.append(link, ' ');
    }
    const receipts = document.createElement('a');
    receipts.href = projectPath('autonomy', {}, projectId);
    receipts.textContent = 'Inspect provider receipts';
    detail.append(receipts);
  }).catch(() => {}); // Metadata stays visible if navigation is unavailable.
}

// At most one pending read per Thread. Event bursts coalesce; the selected
// Thread also refreshes every 30 seconds to observe transitions during a read.
export function createRunnerRefresh({read, apply, onError = () => {}, delay = 5000, timeout = 10000}) {
  const pending = new Map();
  return function refresh(threadId) {
    if (!threadId || pending.has(threadId)) return;
    pending.set(threadId, true);
    setTimeout(async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const thread = await read(threadId, {signal:controller.signal});
        if (!thread.runner || thread.runner.schemaVersion !== '1.0') throw Error('Current custody unavailable');
        if (applyRunner(thread)) apply({...thread, runner: runnerFor(threadId)});
      } catch (error) {
        applyRunner({id: threadId, runner: {schemaVersion:'1.0', state:'unknown', live:false,
          observedAt:Date.now()/1000, reason:'projection_refresh_unavailable'}});
        apply({id:threadId, runner:runnerFor(threadId)});
        onError(threadId, error);
      } finally { clearTimeout(timer); pending.delete(threadId); }
    }, delay);
  };
}

export function createThreadRunnerUI({state, api, patchThread, markThreadBusy, clearThreadBusy,
  scheduleRenderThreads, updateWaitingFromState, renderThreads, logEvent, getMeta, applyQueueAdmissionStatus, setThreadQueueDepth}) {
  let expiryTimer;
  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    const now = Date.now() / 1000;
    const rows = state.threads?.data || state.threads?.threads || state.threads || [];
    const ids = new Set([state.threadId, ...rows.map(row => row.id)]);
    const expiry = Math.min(...[...ids].map(id => runnerFor(id)).filter(runner => runner?.live)
      .map(freshUntil).filter(time => time > now));
    if (Number.isFinite(expiry)) expiryTimer = setTimeout(() => {
      renderThreads();
      if (state.threadId) updateRunnerMeta(state.threadId);
    }, Math.ceil((expiry - now) * 1000) + 1);
  }
  // Events only trigger a read; their unversioned activity cannot establish custody.
  const refreshRunner = createRunnerRefresh({
    read: (threadId, options) => api(`/api/threads/${encodeURIComponent(threadId)}/runner`, options),
    apply: thread => {
      hydrateThreadActivity(thread);
      patchThread(thread.id, {runner:thread.runner, status:thread.status, activity:thread.activity});
      renderThreads();
      if (thread.id === state.threadId) { updateRunnerMeta(thread.id); updateWaitingFromState(); }
    },
    onError: (threadId, error) => logEvent('runner.refresh.error', {threadId, message: error.message}),
  });

  setInterval(() => {
    if (!document.hidden && state.threadId && runnerFor(state.threadId)) refreshRunner(state.threadId);
  }, 30000);

  function updateRunnerMeta(threadId) {
    const meta = getMeta();
    meta.querySelector('[data-runner-indicator]')?.remove();
    meta.querySelector('[data-runner-failure]')?.remove();
    if (threadId) {
      const badge = document.createElement('span');
      badge.dataset.runnerIndicator = '';
      badge.innerHTML = runnerBadgeHtml(threadId, {waiting:!!state.activeTurnsByThread?.get(threadId)?.size,
        queued:state.queuedDepthByThread?.get(threadId) || 0});
      meta.append(badge);
      appendRunnerFailure(meta, runnerFor(threadId), state.projectId);
    }
    scheduleExpiry();
  }

  function hydrateThreadActivity(thread) {
    if (!thread?.id) return;
    applyRunner(thread);
    clearThreadBusy(thread.id);
    if (!allowsRuntimeActivity(thread.id)) { scheduleRenderThreads(); updateWaitingFromState(); return; }
    if (thread.status?.type === "active" || thread.status?.type === "running") {
      markThreadBusy(thread.id, thread.activity?.turnId);
    }
    // An explicit idle/unloaded snapshot takes precedence over retained turns.
    (thread.status || runnerFor(thread.id) ? [] : thread.turns || []).forEach((turn) => {
      if (turn?.status === "inProgress") markThreadBusy(thread.id, turn.id);
    });
    updateWaitingFromState();
  }

  function hydrateThreadListActivity(threads) {
    (threads || []).forEach((thread) => {
      if (!thread?.id) return;
      applyRunner(thread);
      if (!allowsRuntimeActivity(thread.id)) { clearThreadBusy(thread.id); return; }
      if (!thread.status?.type) return;
      if (thread.status.type === "active" || thread.status.type === "running") {
        markThreadBusy(thread.id, thread.activity?.turnId);
      } else if (thread.status.type === "idle" || thread.status.type === "systemError" || thread.status.type === "notLoaded") {
        clearThreadBusy(thread.id);
      }
    });
    updateWaitingFromState();
    scheduleExpiry();
  }

  function updateThreadActivityFromEvent(message) {
    const threadId = message?.params?.threadId || message?.params?.turn?.threadId;
    if (!threadId) return;
    const method = message.method;
    const turnId = message?.params?.turnId || message?.params?.turn?.id;
    if (runnerFor(threadId)) { refreshRunner(threadId); return; }
    if (method === "turn/started" || method === "item/started") {
      markThreadBusy(threadId, turnId);
    } else if (method === "turn/completed" || method === "turn/failed") {
      clearThreadBusy(threadId, turnId);
    } else if (method === "thread/status/changed") {
      const statusType = message.params?.status?.type;
      if (statusType === "active") {
        markThreadBusy(threadId);
      } else if (statusType === "idle" || statusType === "systemError" || statusType === "notLoaded") {
        clearThreadBusy(threadId);
      }
    }
  }

  async function refreshQueueStatus(threadId = state.threadId) {
    if (!threadId) return;
    try {
      const status = await api(`/api/threads/${threadId}/queue`);
      applyQueueAdmissionStatus(state,status);
      setThreadQueueDepth(threadId, status.queueDepth || 0);
      if (status.active && allowsRuntimeActivity(threadId)) markThreadBusy(threadId);
      return status;
    } catch (error) {
      logEvent("queue.error", { message: error.message });
    }
  }

  return {refreshRunner, updateRunnerMeta, hydrateThreadActivity, hydrateThreadListActivity, updateThreadActivityFromEvent, refreshQueueStatus};
}
