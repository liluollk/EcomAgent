# Foundation and Browser Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the TypeScript workspace and a browser-only Craft-derived shell that builds without Electron or Node shims.

**Architecture:** npm workspaces provide strict package boundaries. Shared contracts are created first, then browser-safe design tokens and components are transplanted from the pinned Craft archive into `packages/ui`; `apps/web` owns routing, state, and a typed `WebApiClient` interface.

**Tech Stack:** npm workspaces, TypeScript 5.9.3, Vitest 4.1.9, React 18.3.1, Vite 6.2.4, Playwright 1.63.0.

---

### Task 1: Create the workspace and architecture guard

**Files:**
- Create: `package.json`
- Create: `tsconfig.base.json`
- Create: `vitest.config.ts`
- Create: `.nvmrc`
- Create: `tests/architecture/workspace.test.ts`
- Create: `scripts/verify-boundaries.mjs`

- [ ] **Step 1: Write the failing workspace test**

```ts
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('workspace', () => {
  it('declares the three apps and package workspaces', () => {
    const pkg = JSON.parse(readFileSync('package.json', 'utf8'));
    expect(pkg.workspaces).toEqual(['apps/*', 'packages/*']);
    expect(pkg.engines.node).toBe('>=22.19.0');
  });
});
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `npx vitest run tests/architecture/workspace.test.ts`

Expected: FAIL because the root `package.json` does not exist.

- [ ] **Step 3: Add the minimal workspace files**

Use this root shape and let `npm install` write the lockfile:

```json
{
  "name": "ecomagent",
  "private": true,
  "type": "module",
  "workspaces": ["apps/*", "packages/*"],
  "engines": { "node": ">=22.19.0" },
  "scripts": {
    "typecheck": "npm run typecheck --workspaces --if-present",
    "test": "vitest run",
    "test:integration": "vitest run tests/integration",
    "test:web": "playwright test",
    "build:web": "npm --workspace @ecomagent/web run build",
    "build": "npm run build --workspaces --if-present",
    "verify:boundaries": "node scripts/verify-boundaries.mjs"
  },
  "devDependencies": {
    "@playwright/test": "1.63.0",
    "@types/node": "24.12.4",
    "tsx": "4.20.5",
    "typescript": "5.9.3",
    "vitest": "4.1.9"
  }
}
```

The boundary script must recursively scan `apps/` and `packages/` and fail on `electron`, `window.electronAPI`, imports of Node builtins from browser files, or paths containing `apps/electron`.

- [ ] **Step 4: Install and verify**

Run: `npm install && npx vitest run tests/architecture/workspace.test.ts`

Expected: PASS and `package-lock.json` created.

- [ ] **Step 5: Commit**

```powershell
git add package.json package-lock.json tsconfig.base.json vitest.config.ts .nvmrc scripts/verify-boundaries.mjs tests/architecture/workspace.test.ts
git commit -m "build: create TypeScript workspace"
```

### Task 2: Define shared browser/server contracts

**Files:**
- Create: `packages/shared/package.json`
- Create: `packages/shared/tsconfig.json`
- Create: `packages/shared/src/events.ts`
- Create: `packages/shared/src/rpc.ts`
- Create: `packages/shared/src/index.ts`
- Test: `packages/shared/src/events.test.ts`

- [ ] **Step 1: Write the failing event schema test**

```ts
import { describe, expect, it } from 'vitest';
import { agentEventSchema } from './events.js';

describe('agentEventSchema', () => {
  it('requires a monotonic sequence and workspace scope', () => {
    const result = agentEventSchema.safeParse({
      id: 'evt-1', sequence: 1, workspaceId: 'ws-1', appSessionId: 's-1',
      type: 'approval_requested', occurredAt: '2026-09-18T00:00:00.000Z', payload: {},
    });
    expect(result.success).toBe(true);
  });
});
```

- [ ] **Step 2: Verify the test fails**

Run: `npx vitest run packages/shared/src/events.test.ts`

Expected: FAIL because `events.ts` is missing.

- [ ] **Step 3: Implement the contracts**

Define `AgentEventType` with every event from the approved design, `AgentEvent`, `RpcRequest`, `RpcResponse`, `RpcPush`, `Decision = 'allow' | 'ask' | 'deny'`, and `ExecutionState`. Export only parsed Zod types from `src/index.ts`.

```ts
export const agentEventSchema = z.object({
  id: z.string().min(1),
  sequence: z.number().int().positive(),
  workspaceId: z.string().min(1),
  appSessionId: z.string().min(1),
  type: agentEventTypeSchema,
  occurredAt: z.string().datetime(),
  payload: z.record(z.string(), z.unknown()),
});
```

- [ ] **Step 4: Run unit tests and typecheck**

Run: `npx vitest run packages/shared && npm --workspace @ecomagent/shared run typecheck`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add packages/shared
git commit -m "feat: add shared event and RPC contracts"
```

### Task 3: Transplant browser-safe Craft UI primitives

**Files:**
- Create: `packages/ui/package.json`
- Create: `packages/ui/src/styles/`
- Create: `packages/ui/src/components/chat/`
- Create: `packages/ui/src/components/markdown/`
- Create: `packages/ui/src/components/ui/`
- Create: `packages/ui/src/index.ts`
- Create: `packages/ui/CRAFT_SOURCE.md`
- Test: `tests/architecture/browser-imports.test.ts`

- [ ] **Step 1: Write the failing browser-import test**

The test must scan `packages/ui/src` and fail if source contains `electron`, `window.electronAPI`, `node:fs`, `node:path`, or imports outside `packages/ui` and `packages/shared`.

- [ ] **Step 2: Run it and verify failure**

Run: `npx vitest run tests/architecture/browser-imports.test.ts`

Expected: FAIL because `packages/ui` is absent.

- [ ] **Step 3: Copy and normalize the approved source subset**

Copy design tokens, pure UI controls, chat cards, Markdown/data-table renderers, and styles from:

```text
C:\Users\L3553\Desktop\复现\reference\craft-agents-oss-main\packages\ui\src
```

Do not copy Electron renderer state, transport, filesystem previews, terminal, annotations, or office-document overlays. Replace `@craft-agent/*` imports with `@ecomagent/shared` or local imports. Record upstream version, archive SHA, original paths, and modifications in `CRAFT_SOURCE.md`.

- [ ] **Step 4: Build and scan**

Run: `npm --workspace @ecomagent/ui run typecheck && npx vitest run tests/architecture/browser-imports.test.ts`

Expected: PASS; no Electron or Node builtin browser import.

- [ ] **Step 5: Commit**

```powershell
git add packages/ui tests/architecture/browser-imports.test.ts
git commit -m "feat: add browser-safe Craft UI foundation"
```

### Task 4: Build the browser shell and placeholder pages

**Files:**
- Create: `apps/web/package.json`
- Create: `apps/web/vite.config.ts`
- Create: `apps/web/src/main.tsx`
- Create: `apps/web/src/App.tsx`
- Create: `apps/web/src/api/WebApiClient.ts`
- Create: `apps/web/src/layout/AppShell.tsx`
- Create: `apps/web/src/pages/*.tsx`
- Test: `tests/web/shell.spec.ts`
- Test: `tests/web/craft-parity.spec.ts`

- [ ] **Step 1: Write the failing Playwright shell test**

```ts
import { test, expect } from '@playwright/test';

test('shows the browser-only product navigation', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('navigation')).toContainText('Skills');
  await expect(page.getByRole('navigation')).toContainText('Sources');
  await expect(page.getByRole('navigation')).toContainText('MCP');
  await expect(page.locator('body')).not.toContainText('Electron');
});
```

Add screenshot assertions for the three-column shell, empty workbench, active conversation, Tool card, and right detail panel. Store the approved browser baselines under `tests/web/craft-parity.spec.ts-snapshots/` at desktop `1440x900` and laptop `1280x720` viewports.

- [ ] **Step 2: Verify failure**

Run: `npm run test:web -- tests/web/shell.spec.ts`

Expected: FAIL because the app does not exist.

- [ ] **Step 3: Implement the shell**

Create the three-column shell and routes for Workbench, Sessions, Approvals, Replay, Evaluation, Rules, Skills, Sources, MCP, Automations, and Settings. Preserve the transplanted Craft spacing, typography, colors, navigation density, conversation cards, composer, and detail-panel behavior; change product wording and business content without redesigning the visual system. `WebApiClient` is an interface with `request()` and `subscribe()`; use a local in-memory implementation only for this milestone.

- [ ] **Step 4: Build, scan, and run Playwright**

Run: `npm run build:web && npm run verify:boundaries && npm run test:web -- tests/web/shell.spec.ts tests/web/craft-parity.spec.ts`

Expected: all commands exit `0`.

- [ ] **Step 5: Commit**

```powershell
git add apps/web tests/web playwright.config.ts
git commit -m "feat: add browser-only application shell"
```

**Plan 1 exit gate:** `npm run build:web`, `npm run verify:boundaries`, shared tests, and the shell Playwright test all pass.
