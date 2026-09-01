import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // The scrapers read a DOM, so the tests need one. Node has no DOMParser.
    environment: "happy-dom",
    include: ["**/*.test.js"],
  },
});
