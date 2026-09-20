import { observeRequest, responseBytes } from './frontend_perf.js';

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
  const startedAt = performance.now();
  const method = String(options.method || 'GET').toUpperCase();
  let response = null;
  let text = '';
  let parseMs = 0;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
    });
    text = await response.text();
    let payload = null;
    if (text) {
      const parseStartedAt = performance.now();
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      } finally {
        parseMs = performance.now() - parseStartedAt;
      }
    }
    observeRequest({
      path,
      method,
      status: response.status,
      bytes: responseBytes(text),
      latencyMs: performance.now() - startedAt,
      parseMs,
    });
    if (!response.ok) {
      const detail = (
        payload
        && typeof payload === 'object'
        && 'detail' in payload
      )
        ? payload.detail
        : payload;
      const message = typeof detail === 'string'
        ? detail
        : (
          detail?.code
          || response.statusText
          || `HTTP ${response.status}`
        );
      throw new CodexApiError(message, {
        status: response.status,
        detail,
        path,
      });
    }
    return payload;
  } catch (error) {
    if (error?.name === 'AbortError') {
      observeRequest({
        path,
        method,
        status: response?.status || 0,
        bytes: responseBytes(text),
        latencyMs: performance.now() - startedAt,
        parseMs,
        aborted: true,
      });
    } else if (response === null) {
      observeRequest({
        path,
        method,
        latencyMs: performance.now() - startedAt,
      });
    }
    throw error;
  }
}
