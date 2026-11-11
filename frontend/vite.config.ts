import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `yarn dev` serves on port 8081 and passes API calls to the bench. Log in on the bench's own address
// first: the session cookie belongs to the host, so it is sent from either port.
const bench = process.env.BENCH_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 8081,
    proxy: { "^/(api|assets|login)": { target: bench, changeOrigin: true } },
  },
  build: {
    outDir: "../accounts_intelligence/public/frontend",
    emptyOutDir: true,
  },
});
