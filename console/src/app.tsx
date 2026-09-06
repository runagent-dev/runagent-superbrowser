import { useEffect } from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';
import { subscribeEvents } from './lib/api';
import { ToastProvider, useToast } from './kit';
import { Dashboard } from './screens/Dashboard';
import { Onboarding } from './screens/Onboarding';
import { Sessions } from './screens/Sessions';
import { Handoffs } from './screens/Handoffs';
import { ConfigEditor } from './screens/ConfigEditor';
import { Diagnostics } from './screens/Diagnostics';

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/onboarding', label: 'Onboarding' },
  { to: '/sessions', label: 'Sessions' },
  { to: '/handoffs', label: 'Handoffs' },
  { to: '/config', label: 'Config' },
  { to: '/diagnostics', label: 'Diagnostics' },
];

// Surface live handoff/pairing events as toasts from anywhere in the app.
function GlobalEvents() {
  const toast = useToast();
  useEffect(() => {
    return subscribeEvents((e) => {
      if (e.topic === 'handoff.new') {
        toast({ tone: 'warn', body: `Human needed: ${e.data.caption || e.data.assistType}`, href: e.data.url });
      } else if (e.topic === 'pairing.new') {
        toast({ tone: 'warn', body: `New pairing request: ${e.data.channel}:${e.data.sender_id} (${e.data.code})` });
      } else if (e.topic === 'channel.status' && e.data.status === 'connected') {
        toast({ tone: 'ok', body: `${e.data.channel} connected` });
      }
    });
  }, [toast]);
  return null;
}

export function App() {
  return (
    <ToastProvider>
      <GlobalEvents />
      <AppShell />
    </ToastProvider>
  );
}

function AppShell() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <div className="flex items-center gap-2">
            <span className="inline-block h-4 w-4 rounded-forge" style={{ background: 'var(--sb-ember)' }} />
            <span className="text-sm font-bold uppercase tracking-widest">SuperBrowser</span>
            <span className="mono text-[10px] text-ink-muted">CONSOLE</span>
          </div>
          <nav className="flex gap-1">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `rounded-forge px-3 py-1.5 text-xs font-medium uppercase tracking-wide transition-colors duration-100 ${
                    isActive ? 'bg-orange-tint text-orange-press' : 'text-ink-muted hover:text-ink'
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <main className="forge-grid min-h-[calc(100vh-53px)]">
        <div className="mx-auto max-w-6xl px-6 py-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/onboarding" element={<Onboarding />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/handoffs" element={<Handoffs />} />
            <Route path="/config" element={<ConfigEditor />} />
            <Route path="/diagnostics" element={<Diagnostics />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
