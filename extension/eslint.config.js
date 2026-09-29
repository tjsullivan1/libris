import js from "@eslint/js";
import globals from "globals";

// The popup's tests cover the paths they drive and no others, so `no-undef` is
// still what stands under a typo'd identifier in code with no build step.
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
