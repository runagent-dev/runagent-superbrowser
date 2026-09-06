import React, { createContext, useCallback, useContext, useState } from 'react';

// ---- Card with corner-tick brackets ----
export function Card({
  title,
  index,
  children,
  className = '',
}: {
  title?: string;
  index?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`forge-card p-5 ${className}`}>
      {title && (
        <div className="mb-3 flex items-baseline gap-2">
          {index && <span className="mono text-xs text-orange">{index} /</span>}
          <h2 className="text-sm font-semibold uppercase tracking-wide text-ink">{title}</h2>
        </div>
      )}
      {children}
    </div>
  );
}

// ---- Button ----
export function Button({
  children,
  variant = 'primary',
  className = '',
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' }) {
  const styles = {
    primary: 'bg-orange text-white hover:bg-orange-press',
    ghost: 'bg-transparent text-ink border border-line hover:border-orange',
    danger: 'bg-transparent text-danger border border-danger/40 hover:bg-danger/5',
  }[variant];
  return (
    <button
      {...rest}
      className={`forge-focus rounded-forge px-3.5 py-2 text-sm font-medium transition-colors duration-100 disabled:opacity-40 ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

// ---- Industrial toggle switch (knurled thumb + inline ON/OFF caption) ----
export function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <span className="inline-flex items-center gap-2">
      <button
        onClick={() => onChange(!on)}
        className={`forge-focus relative h-5 w-10 rounded-forge border transition-colors duration-100 ${
          on ? 'border-orange bg-orange-tint' : 'border-line bg-surface'
        }`}
        aria-pressed={on}
      >
        <span
          className={`absolute top-0.5 h-3.5 w-3.5 rounded-forge transition-all duration-100 ${
            on ? 'left-5 bg-orange' : 'left-0.5 bg-ink-muted'
          }`}
          style={{ backgroundImage: 'repeating-linear-gradient(90deg, rgba(0,0,0,.15) 0 1px, transparent 1px 3px)' }}
        />
      </button>
      <span className="mono w-6 text-[9px] text-ink-muted">{on ? 'ON' : 'OFF'}</span>
    </span>
  );
}

// ---- Secret input (write-only, shows last4 when already set) ----
export function SecretInput({
  value,
  placeholder,
  onChange,
}: {
  value: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  return (
    <input
      type="password"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="forge-focus mono w-full rounded-forge border border-line bg-white px-3 py-2 text-sm"
    />
  );
}

export function TextInput({
  value,
  placeholder,
  onChange,
}: {
  value: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  return (
    <input
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="forge-focus w-full rounded-forge border border-line bg-white px-3 py-2 text-sm"
    />
  );
}

// ---- 270° dial gauge ----
export function Gauge({ label, ok, value }: { label: string; ok: boolean | null; value?: string }) {
  const angle = ok === null ? -135 : ok ? 135 : -45;
  const color = ok === null ? '#A8A29E' : ok ? '#15803D' : '#B91C1C';
  return (
    <div className="flex flex-col items-center gap-1">
      <svg viewBox="0 0 100 70" className="w-24">
        <path d="M14 60 A40 40 0 1 1 86 60" fill="none" stroke="#E7E5E4" strokeWidth="5" />
        <g transform={`rotate(${angle} 50 50)`}>
          <line x1="50" y1="50" x2="50" y2="18" stroke={color} strokeWidth="3" />
        </g>
        <circle cx="50" cy="50" r="4" fill={color} />
      </svg>
      <span className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</span>
      {value && <span className="mono text-[10px] text-ink-muted">{value}</span>}
    </div>
  );
}

// ---- Status badge ----
export function Badge({ tone, children }: { tone: 'ok' | 'warn' | 'bad' | 'muted'; children: React.ReactNode }) {
  const styles = {
    ok: 'bg-ok/10 text-ok',
    warn: 'bg-orange-tint text-orange-press',
    bad: 'bg-danger/10 text-danger',
    muted: 'bg-surface text-ink-muted border border-line',
  }[tone];
  return <span className={`mono rounded-forge px-2 py-0.5 text-[11px] ${styles}`}>{children}</span>;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-ink-muted">{hint}</span>}
    </label>
  );
}

// ---- Tabs ----
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="flex gap-1 border-b border-line">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`forge-focus -mb-px border-b-2 px-3 py-2 text-xs font-medium uppercase tracking-wide transition-colors duration-100 ${
            active === t.id ? 'border-orange text-orange-press' : 'border-transparent text-ink-muted hover:text-ink'
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// ---- Number input ----
export function NumberInput({
  value,
  placeholder,
  onChange,
}: {
  value: number | '';
  placeholder?: string;
  onChange: (v: number | undefined) => void;
}) {
  return (
    <input
      type="number"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
      className="forge-focus mono w-full rounded-forge border border-line bg-white px-3 py-2 text-sm"
    />
  );
}

// ---- Toast system ----
type Toast = { id: number; tone: 'ok' | 'warn' | 'bad'; body: string; href?: string };
const ToastCtx = createContext<(t: Omit<Toast, 'id'>) => void>(() => {});

export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, 'id'>) => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { ...t, id }]);
    setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== id)), 8000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`forge-card p-3 text-sm shadow-lg ${
              t.tone === 'bad' ? 'border-l-4 border-l-danger' : t.tone === 'warn' ? 'border-l-4 border-l-orange' : 'border-l-4 border-l-ok'
            }`}
          >
            <p>{t.body}</p>
            {t.href && (
              <a href={t.href} target="_blank" rel="noreferrer" className="mono mt-1 block text-xs text-orange-press">
                open →
              </a>
            )}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---- Countdown ring (QR rotation) ----
export function Countdown({ seconds, total }: { seconds: number; total: number }) {
  const frac = Math.max(0, Math.min(1, seconds / total));
  const circ = 2 * Math.PI * 9;
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5">
      <circle cx="12" cy="12" r="9" fill="none" stroke="#E7E5E4" strokeWidth="2" />
      <circle
        cx="12"
        cy="12"
        r="9"
        fill="none"
        stroke="#FF4D00"
        strokeWidth="2"
        strokeDasharray={circ}
        strokeDashoffset={circ * (1 - frac)}
        transform="rotate(-90 12 12)"
      />
    </svg>
  );
}
