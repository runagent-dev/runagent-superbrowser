import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Badge, Button, Card } from '../kit';

export function Diagnostics() {
  const [checks, setChecks] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const run = () => {
    setLoading(true);
    api
      .doctor()
      .then((r) => setChecks(r.checks || []))
      .catch(() => setChecks([{ name: 'doctor', status: 'warn', detail: 'unreachable' }]))
      .finally(() => setLoading(false));
  };
  useEffect(run, []);

  return (
    <Card title="Diagnostics" index="01">
      <div className="mb-4">
        <Button onClick={run} disabled={loading}>
          {loading ? 'running…' : 'Re-run checks'}
        </Button>
      </div>
      <div className="space-y-1">
        {checks.map((c: any) => (
          <div key={c.name} className="flex items-center gap-3 border-b border-line py-2 last:border-0">
            <Badge tone={c.status === 'ok' ? 'ok' : 'warn'}>{c.status === 'ok' ? 'OK' : 'WARN'}</Badge>
            <span className="w-32 text-sm font-medium">{c.name}</span>
            <span className="mono flex-1 text-xs text-ink-muted">{c.detail}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}
