import { describe, expect, it } from 'vitest';
import { splitProxyCredentials } from '../src/browser/engine.js';

// Regression: a PROXY_POOL entry with embedded credentials was passed verbatim
// to Chrome's --proxy-server, which rejects credentials in that flag with
// net::ERR_NO_SUPPORTED_PROXIES. Every Tier-1 /session/create then failed with
// HTTP 500; the research sweep survived only by escalating to Tier-3.
describe('splitProxyCredentials', () => {
  it('strips credentials from the launch flag and keeps them for page.authenticate', () => {
    expect(splitProxyCredentials('http://user:s3cret@proxy.example.com:8080')).toEqual({
      server: 'http://proxy.example.com:8080',
      auth: { username: 'user', password: 's3cret' },
    });
  });

  it('leaves a credential-free proxy unchanged', () => {
    expect(splitProxyCredentials('http://proxy.example.com:8080')).toEqual({ server: 'http://proxy.example.com:8080' });
    expect(splitProxyCredentials('socks5://10.0.0.1:1080')).toEqual({ server: 'socks5://10.0.0.1:1080' });
  });

  it('decodes percent-encoded credentials', () => {
    expect(splitProxyCredentials('http://u%40x:p%3Aw@h:1').auth).toEqual({ username: 'u@x', password: 'p:w' });
  });

  it('passes through a value that is not a URL', () => {
    expect(splitProxyCredentials('proxy.example.com:8080')).toEqual({ server: 'proxy.example.com:8080' });
  });
});
