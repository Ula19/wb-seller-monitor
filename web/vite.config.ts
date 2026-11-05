import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// в разработке /api проксируем на локальный uvicorn — тот же origin, без CORS
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
