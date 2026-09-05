import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/sessions': 'http://localhost:8000',
      '/approvals': 'http://localhost:8000',
      '/dashboard': 'http://localhost:8000',
      '/providers': 'http://localhost:8000',
      '/sources': 'http://localhost:8000',
      '/skills': 'http://localhost:8000',
      '/workspaces': 'http://localhost:8000',
      '/mcp': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
});