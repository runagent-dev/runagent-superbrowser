import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Badge, Button, Card } from '../kit';

export function Sessions() {
  const [sessions, setSessions] = useState<any[]>([]);
  const [identities, setIdentities] = useState<any[]>([]);

  const refresh = () => {
    api.sessions().then((r) => setSessions(r.sessions || [])).catch(() => {});
    api.identities().then((r) => setIdentities(r.identities || [])).catch(() => {});
  };
  useEffect(refresh, []);

  return (
    <div className="space-y-6">
      <Card title="Engine sessions" index="01">
        {sessions.length === 0 ? (
          <p className="text-sm text-ink-muted">No live browser sessions.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase text-ink-muted">
                <th className="py-2">Session</th>
                <th>URL</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s: any, i: number) => (
                <tr key={s.id || i} className="border-b border-line last:border-0">
                  <td className="mono py-2 text-xs">{s.id || s.sessionId || '?'}</td>
                  <td className="truncate text-ink-muted">{s.url || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Saved logins (identities)" index="02">
        {identities.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No saved logins. When a human logs in via a handoff link, the site is remembered here.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {identities.map((id: any) => (
              <div key={id.domain} className="forge-card p-4">
                <div className="flex items-start justify-between">
                  <span className="mono text-sm font-semibold">{id.domain}</span>
                  {id.encrypted && <Badge tone="muted">enc</Badge>}
                </div>
                <div className="mono mt-2 text-[11px] text-ink-muted">
                  {id.cookieCount >= 0 ? `${id.cookieCount} cookies` : 'encrypted'}
                  <br />
                  last used {new Date(id.lastUsedAt).toLocaleDateString()}
                </div>
                <Button
                  variant="danger"
                  className="mt-3 w-full"
                  onClick={() => api.forgetIdentity(id.domain).then(refresh)}
                >
                  Forget
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
