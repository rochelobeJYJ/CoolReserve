// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// Module build. Served from http://deckmod.localhost/<id>/<version>/, so asset paths are relative.
// No inline scripts: the module CSP allows script-src 'self' only (catalog.md §3).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true, target: "es2022", assetsInlineLimit: 0, chunkSizeWarningLimit: 1500 },
});
