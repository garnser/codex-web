export function createWorkItemViewScope(state, renderItemList) {
  function resetPaging() {
    state.listController?.abort();
    state.listController = null;
    state.listGeneration += 1;
    state.items = [];
    state.nextCursor = null;
    state.hasMore = false;
    state.windowStart = 0;
    state.pageError = '';
    state.detailPayload = null;
    state.runs = { active: [], items: [], nextCursor: null, hasMore: false, activeTruncated: false };
    clearTimeout(state.runRefreshTimer);
    state.runRefreshTimer = null;
    renderItemList();
    const detail = document.querySelector('.work-item-detail');
    if (detail) detail.innerHTML = '<div class="work-item-empty">Select a work item to inspect its canonical state.</div>';
  }
  function captureScope() {
    const { projectId, selectedRef, listGeneration } = state;
    return () => projectId === state.projectId
      && selectedRef === state.selectedRef
      && listGeneration === state.listGeneration;
  }
  function scopedPath(path) {
    const separator = path.includes('?') ? '&' : '?';
    return `${path}${separator}project_id=${encodeURIComponent(state.projectId)}`;
  }
  return { resetPaging, captureScope, scopedPath };
}

export function workItemLoadError(error) {
  const code = String(error?.detail?.code || '').toLowerCase();
  const message = error?.message || 'Failed to load Work Items';
  if (error?.status === 504) return `Backend timeout: ${message}`;
  if (
    code.includes('readiness')
    || code.includes('migration')
    || code.includes('bootstrap')
  ) {
    return `Project readiness blocker: ${message}`;
  }
  return message;
}
