import { useEffect, useState } from 'react';
import { api, subscribeEvents } from '../lib/api';
import { Badge, Button, Card } from '../kit';

export function Handoffs() {
  const [handoffs, setHandoffs] = useState<any[]>([]);

  const refresh = () => api.handoffs().then((r) => setHandoffs(r.handoffs || [])).catch(() => {});

  useEffect(() => {
    refresh();
    const unsub = subscribeEvents((e) => {
      if (e.topic === 'handoff.new') refresh();
    });
    return unsub;
  }, []);

  return (
    <Card title="Handoff inbox" index="01">
      <p className="mb-4 text-sm text-ink-muted">
        When the agent needs a human (captcha, login, approval), the request lands here and is pushed to the active
        chat. Open the live view to help, then the agent resumes automatically.
      </p>
      {handoffs.length === 0 ? (
        <p className="text-sm text-ink-muted">No handoff requests.</p>
      ) : (
        <div className="space-y-3">
          {handoffs.map((h: any) => (
            <div key={h.id} className="forge-card flex gap-4 p-4">
              {h.screenshotPath ? (
                <div className="flex h-24 w-32 items-center justify-center border border-line bg-white text-[10px] text-ink-muted">
                  screenshot saved
                </div>
              ) : null}
              <div className="flex-1">
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone="warn">{h.assistType}</Badge>
                  {h.acked && <Badge tone="ok">acked</Badge>}
                  <span className="mono text-[11px] text-ink-muted">{h.pageTitle || h.pageUrl}</span>
                </div>
                <p className="text-sm">{h.caption}</p>
                <div className="mt-2 flex gap-2">
                  {h.url && (
                    <a href={h.url} target="_blank" rel="noreferrer">
                      <Button variant="primary">Open live view</Button>
                    </a>
                  )}
                  {!h.acked && (
                    <Button variant="ghost" onClick={() => api.ackHandoff(h.id).then(refresh)}>
                      Acknowledge
                    </Button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
