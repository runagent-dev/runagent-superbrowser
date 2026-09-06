import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Badge, Button, Card, Gauge } from '../kit';

export function Dashboard() {
  const [health, setHealth] = useState<any>(null);
  const [sessions, setSessions] = useState<any[]>([]);
  const [error, setError] = useState('');

  const refresh = () => {
    api.health().then(setHealth).catch((e) => setError(String(e)));
    api.sessions().then((r) => setSessions(r.sessions || [])).catch(() => {});
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const engineOk = health ? !!health.engine : null;
  const brainOk = health ? health.channels !== undefined : null;
  const gatewayOk = health ? health.gateway === 'ok' : null;
  const turns: any[] = health?.activeTurns || [];

  return (
    <div className="space-y-6">
      <Card title="System" index="01">
        <div className="flex flex-wrap items-center justify-around gap-6">
          <Gauge label="Gateway" ok={gatewayOk} value={gatewayOk ? 'up' : 'down'} />
          <Gauge label="Engine" ok={engineOk} value={engineOk ? ':3100' : 'offline'} />
          <Gauge label="Channels" ok={brainOk} value={`${Object.keys(health?.channels || {}).length} enabled`} />
        </div>
        {error && <p className="mono mt-4 text-xs text-danger">{error}</p>}
      </Card>

      <Card title="Channels" index="02">
        <div className="space-y-2">
          {Object.entries(health?.channels || {}).map(([name, status]) => (
            <div key={name} className="flex items-center justify-between border-b border-line py-2 last:border-0">
              <span className="text-sm font-medium capitalize">{name}</span>
              <Badge tone={status === 'connected' ? 'ok' : status === 'waiting_qr' ? 'warn' : 'muted'}>
                {String(status)}
              </Badge>
            </div>
          ))}
          {Object.keys(health?.channels || {}).length === 0 && (
            <p className="text-sm text-ink-muted">No channels enabled — set them up in Onboarding.</p>
          )}
        </div>
      </Card>

      <Card title="Live browser sessions" index="03">
        {sessions.length === 0 ? (
          <p className="text-sm text-ink-muted">No live browser session. Live-view links appear here while a task browses.</p>
        ) : (
          <div className="space-y-2">
            {sessions.map((s: any, i: number) => {
              const sid = s.id || s.sessionId;
              return (
                <div key={sid || i} className="flex items-center justify-between border-b border-line py-2 last:border-0">
                  <div className="min-w-0">
                    <span className="mono text-xs">{sid}</span>
                    <span className="ml-2 truncate text-xs text-ink-muted">{s.url || ''}</span>
                  </div>
                  {s.viewUrl && (
                    <a href={s.viewUrl} target="_blank" rel="noreferrer">
                      <Button variant="ghost">Live view</Button>
                    </a>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card title="Active tasks" index="04">
        {turns.length === 0 ? (
          <p className="text-sm text-ink-muted">Nothing running.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase text-ink-muted">
                <th className="py-2">Chat</th>
                <th>Task</th>
                <th className="text-right">Elapsed</th>
                <th className="text-right">Stop</th>
              </tr>
            </thead>
            <tbody>
              {turns.map((t) => (
                <TaskRow key={t.session_key} turn={t} onStopped={refresh} />
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function TaskRow({ turn, onStopped }: { turn: any; onStopped: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const elapsed = Math.round((Date.now() / 1000 - turn.started_at) / 60);
  return (
    <tr className="border-b border-line last:border-0">
      <td className="mono py-2 text-xs">{turn.session_key}</td>
      <td className="text-ink-muted">{turn.last_inbound || '(task)'}</td>
      <td className="mono text-right text-xs">{elapsed}m</td>
      <td className="py-2 text-right">
        {confirming ? (
          <Button
            variant="danger"
            onClick={() => api.stopTask(turn.session_key).then(onStopped)}
          >
            confirm
          </Button>
        ) : (
          <Button variant="ghost" onClick={() => setConfirming(true)}>
            stop
          </Button>
        )}
      </td>
    </tr>
  );
}
