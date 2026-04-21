import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import { componentTagger } from "lovable-tagger";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  base: "/admin/",
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        chat: path.resolve(__dirname, "chat.html"),
      },
    },
  },
  server: {
    host: "::",
    port: 8080,
    hmr: {
      overlay: false,
      port: 8080,
      host: "localhost",
    },
    proxy: {
      // KB scraper API goes direct to port 8501 — no NexOria hop needed
      "/kb-scraper/api": {
        target: "http://localhost:8501",
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/kb-scraper/, ""),
      },
      // NexOria admin & chat APIs
      "/admin/api": { target: "http://localhost:8011", changeOrigin: true },
      "/api": { target: "http://localhost:8011", changeOrigin: true },
      // Widget loader and static assets served by FastAPI
      "/static": { target: "http://localhost:8011", changeOrigin: true },
    },
  },
  plugins: [react(), mode === "development" && componentTagger()].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
  },
}));
