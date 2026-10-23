// One script and one stylesheet with fixed names, served by counter_app.py from /static/.
// No CDN, no code splitting, no inline scripts: the counter app's CSP allows script-src 'self' only,
// and the counter PC may be offline. The runtime-only Vue build is used (templates compiled here),
// so the page never needs 'unsafe-eval'.
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  define: { __VUE_OPTIONS_API__: false, __VUE_PROD_DEVTOOLS__: false, __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: false },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    target: 'es2019',
    modulePreload: false,
    cssCodeSplit: false,
    rollupOptions: {
      input: 'src/main.js',
      output: {
        entryFileNames: 'app.js',
        assetFileNames: 'app[extname]',
        inlineDynamicImports: true,
      },
    },
  },
})
