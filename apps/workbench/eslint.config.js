import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

/**
 * Workbench 最小 ESLint 配置(阶段 4 Gate;计划 04 Task 10 Step 4 的 lint)。
 *
 * 规则从简:recommended + react-hooks;类型规则只开 recommended(非
 * strict),让 `npm run lint` 可以稳定作为门禁运行。
 */
export default tseslint.config(
  { ignores: ["dist", "node_modules", "playwright-report", "test-results"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.es2022 },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // 测试文件里的宽松约定:允许非空断言与 any(最小门禁,不追逐严格)。
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
