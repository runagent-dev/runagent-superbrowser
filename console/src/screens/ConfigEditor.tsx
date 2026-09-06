import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Badge, Button, Card, Field, NumberInput, SecretInput, Switch, Tabs, TextInput, useToast } from '../kit';

type Cfg = Record<string, any>;

export function ConfigEditor() {
  const [config, setConfig] = useState<Cfg>({});
  const [text, setText] = useState('');
  const [effective, setEffective] = useState<any[]>([]);
  const [tab, setTab] = useState('form');
  const [restart, setRestart] = useState<string[]>([]);
  const toast = useToast();

  const load = () =>
    api.config().then((r) => {
      setConfig(r.config || {});
      setText(JSON.stringify(r.config, null, 2));
      setEffective(r.effective || []);
    });
  useEffect(() => {
    load().catch((e) => toast({ tone: 'bad', body: String(e) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async (payload: Cfg) => {
    try {
      const r = await api.saveConfig(payload);
      setRestart(r.requiresRestart || []);
      toast({ tone: r.warnings?.length ? 'warn' : 'ok', body: r.warnings?.length ? r.warnings.join('; ') : 'Saved.' });
      load();
    } catch (e) {
      toast({ tone: 'bad', body: String(e) });
    }
  };

  return (
    <div className="space-y-4">
      {restart.length > 0 && (
        <div className="forge-card border-l-4 border-l-orange p-3 text-sm">
          Saved — restart the <b>{restart.join(', ')}</b> to apply (config changes take effect on next start).
        </div>
      )}
      <Tabs
        tabs={[
          { id: 'form', label: 'Form' },
          { id: 'raw', label: 'Raw JSON' },
          { id: 'effective', label: 'Effective' },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'form' && <FormView config={config} onSave={save} />}
      {tab === 'raw' && <RawView text={text} setText={setText} onSave={save} />}
      {tab === 'effective' && <EffectiveView effective={effective} />}
    </div>
  );
}

function set(obj: Cfg, path: string, value: any): Cfg {
  const next = structuredClone(obj);
  const parts = path.split('.');
  let node = next;
  for (const p of parts.slice(0, -1)) {
    node[p] = node[p] && typeof node[p] === 'object' ? node[p] : {};
    node = node[p];
  }
  if (value === undefined || value === '') delete node[parts[parts.length - 1]];
  else node[parts[parts.length - 1]] = value;
  return next;
}
const get = (obj: Cfg, path: string): any =>
  path.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), obj);

function FormView({ config, onSave }: { config: Cfg; onSave: (c: Cfg) => void }) {
  const [draft, setDraft] = useState<Cfg>(config);
  useEffect(() => setDraft(config), [config]);

  const S = (path: string, v: any) => setDraft((d) => set(d, path, v));
  const secret = (path: string) => {
    const v = get(draft, path);
    return typeof v === 'string' ? v : '';
  };
  const secretPlaceholder = (path: string) => {
    const v = get(config, path);
    return v && v.set ? `set ••••${v.last4}` : '';
  };

  return (
    <div className="space-y-4">
      <Card title="Profile" index="00">
        <Field label="Machine profile" hint="local = laptop · vm = headless server · docker = container">
          <select
            value={get(draft, 'profile') || ''}
            onChange={(e) => S('profile', e.target.value || undefined)}
            className="forge-focus rounded-forge border border-line bg-white px-3 py-2 text-sm"
          >
            <option value="">(auto-detect)</option>
            <option value="local">local</option>
            <option value="vm">vm</option>
            <option value="docker">docker</option>
          </select>
        </Field>
      </Card>

      <Card title="Brain (LLM)" index="01">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Provider">
            <TextInput value={get(draft, 'brain.provider') || ''} placeholder="anthropic" onChange={(v) => S('brain.provider', v)} />
          </Field>
          <Field label="Model">
            <TextInput value={get(draft, 'brain.model') || ''} placeholder="claude-sonnet-5" onChange={(v) => S('brain.model', v)} />
          </Field>
          <Field label="API key">
            <SecretInput value={secret('brain.apiKey')} placeholder={secretPlaceholder('brain.apiKey')} onChange={(v) => S('brain.apiKey', v)} />
          </Field>
          <Field label="Base URL (optional)">
            <TextInput value={get(draft, 'brain.baseUrl') || ''} placeholder="" onChange={(v) => S('brain.baseUrl', v)} />
          </Field>
        </div>
      </Card>

      <Card title="Vision (Gemini)" index="02">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex items-center gap-3">
            <Switch on={get(draft, 'vision.enabled') !== false} onChange={(v) => S('vision.enabled', v)} />
            <span className="text-sm">Vision preprocessor enabled</span>
          </div>
          <Field label="API key (separate from brain)">
            <SecretInput value={secret('vision.apiKey')} placeholder={secretPlaceholder('vision.apiKey')} onChange={(v) => S('vision.apiKey', v)} />
          </Field>
          <Field label="Model">
            <TextInput value={get(draft, 'vision.model') || ''} placeholder="gemini-2.0-flash-exp" onChange={(v) => S('vision.model', v)} />
          </Field>
          <Field label="Cache TTL (sec)">
            <NumberInput value={get(draft, 'vision.cacheTtlSec') ?? ''} placeholder="30" onChange={(v) => S('vision.cacheTtlSec', v)} />
          </Field>
        </div>
      </Card>

      <Card title="Engine" index="03">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Port"><NumberInput value={get(draft, 'engine.port') ?? ''} placeholder="3100" onChange={(v) => S('engine.port', v)} /></Field>
          <Field label="Auth token (unlocks /script)"><SecretInput value={secret('engine.token')} placeholder={secretPlaceholder('engine.token')} onChange={(v) => S('engine.token', v)} /></Field>
          <div className="flex items-center gap-3">
            <Switch on={get(draft, 'engine.headless') !== false} onChange={(v) => S('engine.headless', v)} />
            <span className="text-sm">Headless</span>
          </div>
          <Field label="Chrome path" hint='"auto" detects a real Chrome'>
            <TextInput value={get(draft, 'engine.chromePath') || ''} placeholder="auto" onChange={(v) => S('engine.chromePath', v)} />
          </Field>
        </div>
      </Card>

      <Card title="Anti-bot & identities" index="04">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex items-center gap-3">
            <Switch on={!!get(draft, 'antibot.cookieJar')} onChange={(v) => S('antibot.cookieJar', v)} />
            <span className="text-sm">Bot-protection cookie jar</span>
          </div>
          <div className="flex items-center gap-3">
            <Switch on={!!get(draft, 'identities.enabled')} onChange={(v) => S('identities.enabled', v)} />
            <span className="text-sm">Identity jar (stay logged in)</span>
          </div>
          <Field label="Max human handoffs / session">
            <NumberInput value={get(draft, 'antibot.maxHumanHandoffs') ?? ''} placeholder="1" onChange={(v) => S('antibot.maxHumanHandoffs', v)} />
          </Field>
          <Field label="Identity TTL (days)">
            <NumberInput value={get(draft, 'identities.ttlDays') ?? ''} placeholder="30" onChange={(v) => S('identities.ttlDays', v)} />
          </Field>
        </div>
      </Card>

      <Card title="Public host (handoff links)" index="05">
        <Field label="Public base URL" hint="Required on a VM so live-view links open from a phone. Leave blank for localhost.">
          <TextInput value={get(draft, 'publicHost') || ''} placeholder="https://browser.example.com" onChange={(v) => S('publicHost', v)} />
        </Field>
      </Card>

      <div className="flex items-center gap-3">
        <Button onClick={() => onSave(draft)}>Save configuration</Button>
        <span className="text-xs text-ink-muted">Channels are configured in Onboarding.</span>
      </div>
    </div>
  );
}

function RawView({ text, setText, onSave }: { text: string; setText: (t: string) => void; onSave: (c: Cfg) => void }) {
  const toast = useToast();
  const parse = (): Cfg | null => {
    try {
      return JSON.parse(text);
    } catch (e) {
      toast({ tone: 'bad', body: `Invalid JSON: ${e}` });
      return null;
    }
  };
  return (
    <Card title="Raw JSON" index="01">
      <p className="mb-2 text-xs text-ink-muted">
        Secrets show as <span className="mono">{'{set, last4}'}</span> — leave them to keep the stored value.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        className="forge-focus mono h-96 w-full rounded-forge border border-line bg-white p-3 text-xs"
      />
      <div className="mt-3 flex gap-3">
        <Button onClick={() => { const c = parse(); if (c) onSave(c); }}>Save</Button>
        <Button
          variant="ghost"
          onClick={async () => {
            const c = parse();
            if (!c) return;
            const r = await api.validateConfig(c);
            toast({
              tone: r.errors?.length ? 'bad' : r.warnings?.length ? 'warn' : 'ok',
              body: r.errors?.join('; ') || r.warnings?.join('; ') || 'Valid.',
            });
          }}
        >
          Validate
        </Button>
      </div>
    </Card>
  );
}

function EffectiveView({ effective }: { effective: any[] }) {
  return (
    <Card title="Effective values" index="01">
      <p className="mb-3 text-xs text-ink-muted">
        What the engine/SDK actually receive. Precedence: environment &gt; .env &gt; config &gt; preset.
      </p>
      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full text-xs">
          <tbody>
            {effective.map((row: any) => (
              <tr key={row.path} className="border-b border-line last:border-0">
                <td className="mono py-1.5 pr-2 text-ink-muted">{row.env.join(',')}</td>
                <td className="mono py-1.5 pr-2">{row.secret ? '••••' : row.value}</td>
                <td className="py-1.5 text-right">
                  <Badge tone={row.source === 'environment' ? 'warn' : 'muted'}>{row.source}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
