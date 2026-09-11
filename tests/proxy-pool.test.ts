import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ProxyPool } from '../src/browser/proxy-pool.js';

/**
 * Two bugs found while configuring a residential proxy against live
 * PerimeterX/Cloudflare sites, both of which left the proxy configured but
 * unused so requests kept going out on the host's datacenter IP.
 */
describe('ProxyPool env parsing', () => {
  const saved = { pool: process.env.PROXY_POOL, dflt: process.env.PROXY_DEFAULT };
  beforeEach(() => { delete process.env.PROXY_POOL; delete process.env.PROXY_DEFAULT; });
  afterEach(() => {
    if (saved.pool === undefined) delete process.env.PROXY_POOL; else process.env.PROXY_POOL = saved.pool;
    if (saved.dflt === undefined) delete process.env.PROXY_DEFAULT; else process.env.PROXY_DEFAULT = saved.dflt;
  });

  const stubEngine = () => ({ newPage: async () => ({}) }) as never;

  it('takes a bare URL whole instead of reading the scheme as a region', () => {
    // Was: region "http", url "//user:pass@host:port" — not a usable proxy.
    process.env.PROXY_POOL = 'http://user:pass@48.44.107.248:45539';
    const pool = new ProxyPool(stubEngine());
    const regions = pool.listRegions ? pool.listRegions() : [...(pool as never as { proxies: Map<string, { url: string }> }).proxies.keys()];
    expect(regions).toEqual(['default']);
    const entry = (pool as never as { proxies: Map<string, { url: string }> }).proxies.get('default');
    expect(entry?.url).toBe('http://user:pass@48.44.107.248:45539');
  });

  it('still supports the documented region:url form, including socks', () => {
    process.env.PROXY_POOL = 'us:http://u:p@a.example:8080,bd:socks5://bd.example:1080';
    const pool = new ProxyPool(stubEngine());
    const proxies = (pool as never as { proxies: Map<string, { url: string }> }).proxies;
    expect([...proxies.keys()].sort()).toEqual(['bd', 'us']);
    expect(proxies.get('us')?.url).toBe('http://u:p@a.example:8080');
    expect(proxies.get('bd')?.url).toBe('socks5://bd.example:1080');
  });

  it('routes a session with no region through the configured proxy', async () => {
    // Was: newPage fell straight through to the direct engine, so a configured
    // pool never applied to an ordinary session and PROXY_DEFAULT was only read
    // when a region had been requested AND missed.
    process.env.PROXY_POOL = 'http://u:p@48.44.107.248:45539';
    const direct = stubEngine();
    const pool = new ProxyPool(direct);
    const asked: string[] = [];
    (pool as never as { getEngine: (u: string) => Promise<unknown> }).getEngine = async (url: string) => {
      asked.push(url);
      return { newPage: async () => ({ via: url }) };
    };
    const page = await pool.newPage();
    expect(asked).toEqual(['http://u:p@48.44.107.248:45539']);
    expect(page).toEqual({ via: 'http://u:p@48.44.107.248:45539' });
  });

  it('leaves sessions direct when nothing is configured', async () => {
    const marker = { newPage: async () => ({ direct: true }) } as never;
    const pool = new ProxyPool(marker);
    expect(await pool.newPage()).toEqual({ direct: true });
  });
});
