import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Build output is packaged into the wheel and served by the gateway
// (superbrowser_gateway/console_dist). Dev proxies /api to the running gateway.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../nanobot/superbrowser_gateway/console_dist',
    emptyOutDir: true,
  },
  server: {
    port: 5273,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8460', changeOrigin: true, ws: true },
    },
  },
});
