export class CodexApiError extends Error {
  constructor(message, { status = 0, detail = null, path = '' } = {}) {
    super(message);
    this.name = 'CodexApiError';
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

export function apiBase() {
  return window.location.pathname.startsWith('/codex') ? '/codex' : '';
}

export async function request(path, options = {}) {
  const response = await fetch(`${apiBase()}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload
      ? payload.detail
      : payload;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.code || response.statusText || `HTTP ${response.status}`);
    throw new CodexApiError(message, {
      status: response.status,
      detail,
      path,
    });
  }
  return payload;
}
