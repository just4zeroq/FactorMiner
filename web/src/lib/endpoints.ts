/**
 * Single source of truth for backend addresses.
 *
 * Historically every page hard-coded http://localhost:8000 (and the websocket
 * hook hard-coded ws://localhost:8000). That only works when the browser runs
 * on the same machine as the API, and it bypasses the nginx reverse proxy in
 * the container image entirely.
 *
 * VITE_API_BASE:
 *   - unset  -> http://localhost:8000 (unchanged `npm run dev` behaviour)
 *   - ""     -> same origin, i.e. /api and /ws go through the nginx proxy (or
 *              the vite dev proxy). The Docker image builds with this value.
 *   - any other explicit URL is used verbatim.
 *
 * VITE_API_TOKEN is only for local / trusted-network deployments: like any
 * VITE_* variable it is inlined into the static bundle, so it is NOT a secret.
 * For a public deployment, terminate authentication at the reverse proxy
 * instead and leave this unset.
 */
const configuredBase = import.meta.env.VITE_API_BASE as string | undefined;

export const API_BASE = (
  configuredBase === undefined ? 'http://localhost:8000' : configuredBase
).replace(/\/+$/, '');

export const API_TOKEN = (import.meta.env.VITE_API_TOKEN as string | undefined) ?? '';

/** fetch() with the optional API token attached. `path` starts with `/`. */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (API_TOKEN && !headers.has('X-API-Token')) {
    headers.set('X-API-Token', API_TOKEN);
  }
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

function sameOriginWsBase(): string {
  if (typeof window === 'undefined') return 'ws://localhost:8000';
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}`;
}

/**
 * Browsers cannot attach custom headers to a WebSocket handshake, so when a
 * token is configured it travels as a query parameter (accepted only for
 * websocket scopes by api/main.py:require_auth).
 */
export const WS_URL = (() => {
  const base = API_BASE ? API_BASE.replace(/^http/, 'ws') : sameOriginWsBase();
  const query = API_TOKEN ? `?token=${encodeURIComponent(API_TOKEN)}` : '';
  return `${base}/ws/monitor${query}`;
})();
