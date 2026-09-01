import js from "@eslint/js";
import globals from "globals";

// With no tests over the popup, `no-undef` is the only thing standing under a
// typo'd identifier in code that has no build step to catch one.
export default [
  { ignores: ["node_modules/**"] },
  js.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser, chrome: "readonly" },
    },
  },
  {
    files: ["**/*.test.js"],
    languageOptions: { globals: globals.node },
  },
];
