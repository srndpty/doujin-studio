import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov", "cobertura"],
      reportsDirectory: "../reports/coverage/frontend",
      include: ["src/editor-geometry.ts", "src/PageEditor.tsx", "src/prompt-preview.ts"],
      exclude: ["src/main.tsx", "src/api/schema.d.ts", "src/test/**"],
      thresholds: { lines: 60, functions: 60, statements: 60, branches: 50 }
    }
  }
});
