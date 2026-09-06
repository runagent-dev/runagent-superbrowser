// Thin fetch wrapper. The token (when the gateway requires one) is read from
// ?token= on first load and kept in sessionStorage for API calls.

const tokenFromUrl = new URLSearchParams(window.location.search).get('token');
if (tokenFromUrl) sessionStorage.setItem('sb_token', tokenFromUrl);

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem('sb_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : {};
  if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
  return data as T;
}

export const api = {
  health: () => req<any>('GET', '/api/health'),
  channels: () => req<any>('GET', '/api/channels'),
  config: () => req<any>('GET', '/api/config'),
  saveConfig: (config: unknown) => req<any>('PUT', '/api/config', { config }),
  validateConfig: (config: unknown) => req<any>('POST', '/api/config/validate', { config }),
  detectProfile: () => req<any>('GET', '/api/profile/detect'),
  pairing: () => req<any>('GET', '/api/pairing'),
  approvePairing: (code: string) => req<any>('POST', '/api/pairing/approve', { code }),
  denyPairing: (code: string) => req<any>('POST', '/api/pairing/deny', { code }),
  tasks: () => req<any>('GET', '/api/tasks'),
  stopTask: (sessionKey: string) => req<any>('POST', `/api/tasks/${encodeURIComponent(sessionKey)}/stop`),
  sessions: () => req<any>('GET', '/api/sessions'),
  identities: () => req<any>('GET', '/api/identities'),
  forgetIdentity: (domain: string) => req<any>('DELETE', `/api/identities/${encodeURIComponent(domain)}`),
  handoffs: () => req<any>('GET', '/api/handoffs'),
  ackHandoff: (id: string) => req<any>('POST', `/api/handoffs/${encodeURIComponent(id)}/ack`),
  doctor: () => req<any>('GET', '/api/doctor'),
  qrUrl: () => {
    const token = sessionStorage.getItem('sb_token');
    return `/api/channels/whatsapp/qr.png${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  },
};

export type ConsoleEvent = { topic: string; data: any };

// Server-Sent Events feed (QR frames, channel status, handoffs, pairing).
export function subscribeEvents(onEvent: (e: ConsoleEvent) => void): () => void {
  const token = sessionStorage.getItem('sb_token');
  const url = `/api/events${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  const es = new EventSource(url);
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch {
      /* ignore keepalives */
    }
  };
  return () => es.close();
}
