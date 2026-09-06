import { useEffect, useState } from 'react';
import { api, subscribeEvents } from '../lib/api';
import { Badge, Button, Card, Countdown, Field, SecretInput, Switch, TextInput, useToast } from '../kit';

export function Onboarding() {
  const [detect, setDetect] = useState<any>(null);
  const [config, setConfig] = useState<any>({});
  const toast = useToast();

  useEffect(() => {
    api.detectProfile().then(setDetect).catch(() => {});
    api.config().then((r) => setConfig(r.config || {})).catch(() => {});
  }, []);

  const gw = config.gateway || {};
  const channels = gw.channels || {};

  const setChannel = (name: string, patch: any) =>
    setConfig((c: any) => ({
      ...c,
      gateway: { ...(c.gateway || {}), enabled: true, channels: { ...channels, [name]: { ...(channels[name] || {}), ...patch } } },
    }));

  const save = async () => {
    try {
      await api.saveConfig(config);
      toast({ tone: 'ok', body: 'Saved. Restart the gateway to apply channel changes.' });
    } catch (e) {
      toast({ tone: 'bad', body: String(e) });
    }
  };

  return (
    <div className="space-y-6">
      <Card title="Detect" index="01">
        {detect ? (
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Stat label="Profile" value={detect.suggestedProfile} />
            <Stat label="Platform" value={detect.platform} />
            <Stat label="Chrome" value={detect.chromeFound ? 'found' : 'bundled'} />
            <Stat label="Display" value={detect.display || '—'} />
          </div>
        ) : (
          <p className="text-sm text-ink-muted">detecting…</p>
        )}
      </Card>

      <Card title="Brain & vision" index="02">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="LLM provider">
            <TextInput
              value={config.brain?.provider || ''}
              placeholder="anthropic"
              onChange={(v) => setConfig((c: any) => ({ ...c, brain: { ...(c.brain || {}), provider: v } }))}
            />
          </Field>
          <Field label="LLM model">
            <TextInput
              value={config.brain?.model || ''}
              placeholder="claude-sonnet-5"
              onChange={(v) => setConfig((c: any) => ({ ...c, brain: { ...(c.brain || {}), model: v } }))}
            />
          </Field>
          <Field label="LLM API key">
            <SecretInput
              value={typeof config.brain?.apiKey === 'string' ? config.brain.apiKey : ''}
              placeholder={config.brain?.apiKey?.set ? `set ••••${config.brain.apiKey.last4}` : 'sk-…'}
              onChange={(v) => setConfig((c: any) => ({ ...c, brain: { ...(c.brain || {}), apiKey: v } }))}
            />
          </Field>
          <Field label="Gemini VISION key">
            <SecretInput
              value={typeof config.vision?.apiKey === 'string' ? config.vision.apiKey : ''}
              placeholder={config.vision?.apiKey?.set ? `set ••••${config.vision.apiKey.last4}` : 'AIza…'}
              onChange={(v) => setConfig((c: any) => ({ ...c, vision: { ...(c.vision || {}), apiKey: v } }))}
            />
          </Field>
        </div>
      </Card>

      <WhatsAppCard channels={channels} setChannel={setChannel} />

      <Card title="Telegram" index="04">
        <ChannelForm
          name="telegram"
          section={channels.telegram || {}}
          setChannel={setChannel}
          tokenLabel="Bot token (@BotFather)"
          allowLabel="Allowed user IDs (comma-separated)"
        />
      </Card>

      <Card title="Discord" index="05">
        <ChannelForm
          name="discord"
          section={channels.discord || {}}
          setChannel={setChannel}
          tokenLabel="Bot token"
          allowLabel="Allowed user IDs (comma-separated)"
        />
      </Card>

      <div className="flex items-center gap-4">
        <Button onClick={save}>Save configuration</Button>
        <span className="text-xs text-ink-muted">Then: superbrowser-gateway login whatsapp, then restart the gateway.</span>
      </div>
    </div>
  );
}

const QR_ROTATE_S = 20;

function WhatsAppCard({ channels, setChannel }: { channels: any; setChannel: (n: string, p: any) => void }) {
  const section = channels.whatsapp || {};
  const [qrError, setQrError] = useState(false);
  const [qrNonce, setQrNonce] = useState(0); // cache-buster: bump to reload the img
  const [qrSecs, setQrSecs] = useState(QR_ROTATE_S);
  const [status, setStatus] = useState<string>('');
  const [pending, setPending] = useState<any[]>([]);

  useEffect(() => {
    const unsub = subscribeEvents((e) => {
      if (e.topic === 'channel.status' && e.data.channel === 'whatsapp') setStatus(e.data.status);
      if (e.topic === 'qr.whatsapp') {
        setQrError(false);
        setQrNonce((n) => n + 1); // a fresh QR frame arrived — reload the image
        setQrSecs(QR_ROTATE_S);
      }
      if (e.topic === 'pairing.new') setPending((p) => [...p, e.data]);
    });
    api.pairing().then((r) => setPending(r.pending || [])).catch(() => {});
    return unsub;
  }, []);

  // While waiting for a scan, poll the QR image every few seconds (SSE also
  // pushes fresh frames) and count down the rotation.
  useEffect(() => {
    if (status === 'connected' || !section.enabled) return;
    const tick = setInterval(() => setQrSecs((s) => (s <= 1 ? QR_ROTATE_S : s - 1)), 1000);
    const poll = setInterval(() => setQrNonce((n) => n + 1), 5000);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
    };
  }, [status, section.enabled]);

  return (
    <Card title="WhatsApp" index="03">
      <div className="mb-4 flex items-center gap-3">
        <Switch on={!!section.enabled} onChange={(v) => setChannel('whatsapp', { enabled: v })} />
        <span className="text-sm">Enable WhatsApp channel</span>
        {status && <Badge tone={status === 'connected' ? 'ok' : status === 'waiting_qr' ? 'warn' : 'muted'}>{status}</Badge>}
      </div>
      {section.enabled && (
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-medium uppercase tracking-wide text-ink-muted">Scan with Linked Devices</span>
              {status === 'connected' ? (
                <Badge tone="ok">linked</Badge>
              ) : !qrError ? (
                <span className="flex items-center gap-1 text-[11px] text-ink-muted">
                  <Countdown seconds={qrSecs} total={QR_ROTATE_S} /> rotates
                </span>
              ) : null}
            </div>
            <div className="forge-card flex aspect-square w-48 items-center justify-center p-2">
              {status === 'connected' ? (
                <span className="mono text-center text-[11px] text-ok">✓ WhatsApp linked</span>
              ) : qrError ? (
                <span className="mono text-center text-[11px] text-ink-muted">
                  No QR yet.
                  <br />
                  Run: superbrowser-gateway login whatsapp
                </span>
              ) : (
                <img
                  key={qrNonce}
                  src={`${api.qrUrl()}${api.qrUrl().includes('?') ? '&' : '?'}n=${qrNonce}`}
                  alt="WhatsApp QR"
                  className="h-full w-full object-contain"
                  onError={() => setQrError(true)}
                />
              )}
            </div>
          </div>
          <div>
            <Field label="Allowed numbers (digits, comma-separated)">
              <TextInput
                value={(section.allowFrom || []).join(', ')}
                placeholder="15551234567"
                onChange={(v) =>
                  setChannel('whatsapp', { allowFrom: v.split(',').map((s) => s.trim().replace(/^\+/, '')).filter(Boolean) })
                }
              />
            </Field>
            {pending.length > 0 && (
              <div className="mt-4">
                <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-ink-muted">
                  Pairing requests
                </span>
                {pending.map((p) => (
                  <div key={p.code} className="flex items-center justify-between border-b border-line py-1.5 text-sm">
                    <span className="mono text-xs">
                      {p.channel}:{p.sender_id} <span className="text-orange">{p.code}</span>
                    </span>
                    <div className="flex gap-1">
                      <Button variant="ghost" onClick={() => api.approvePairing(p.code)}>
                        approve
                      </Button>
                      <Button variant="danger" onClick={() => api.denyPairing(p.code)}>
                        deny
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function ChannelForm({
  name,
  section,
  setChannel,
  tokenLabel,
  allowLabel,
}: {
  name: string;
  section: any;
  setChannel: (n: string, p: any) => void;
  tokenLabel: string;
  allowLabel: string;
}) {
  return (
    <div>
      <div className="mb-4 flex items-center gap-3">
        <Switch on={!!section.enabled} onChange={(v) => setChannel(name, { enabled: v })} />
        <span className="text-sm">Enable {name} channel</span>
      </div>
      {section.enabled && (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={tokenLabel}>
            <SecretInput
              value={typeof section.token === 'string' ? section.token : ''}
              placeholder={section.token?.set ? `set ••••${section.token.last4}` : ''}
              onChange={(v) => setChannel(name, { token: v })}
            />
          </Field>
          <Field label={allowLabel}>
            <TextInput
              value={(section.allowFrom || []).join(', ')}
              onChange={(v) => setChannel(name, { allowFrom: v.split(',').map((s) => s.trim()).filter(Boolean) })}
            />
          </Field>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="forge-card p-3">
      <div className="text-xs uppercase text-ink-muted">{label}</div>
      <div className="mono mt-1 text-sm">{value}</div>
    </div>
  );
}
