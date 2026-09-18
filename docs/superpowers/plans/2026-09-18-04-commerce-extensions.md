# Commerce Execution and Extension Systems Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the safe price-change loop across three controlled platform protocols and make Skills, Sources, MCP, and Automations usable browser features.

**Architecture:** Platform differences live behind one adapter contract and all writes enter through `PriceChangeExecutor.executeAuthorizedIntent()`. Durable jobs use leases, fencing, idempotency, status reconciliation, and manual review; extensions feed the same Pi registry and PreToolUse Pipeline instead of bypassing safety.

**Tech Stack:** TypeScript, SQLite, Express, React, Vitest, Playwright, Pi Coding Agent 0.80.6.

---

### Task 1: Define commerce values and adapter contracts

**Files:**
- Create: `packages/commerce/package.json`
- Create: `packages/commerce/src/money.ts`
- Create: `packages/commerce/src/product.ts`
- Create: `packages/commerce/src/PlatformAdapter.ts`
- Create: `packages/commerce/src/AdapterRegistry.ts`
- Test: `packages/commerce/src/PlatformAdapter.contract.test.ts`

- [ ] **Step 1: Write the failing contract test**

```ts
it.each(adapterFactories)('%s preserves integer money and version checks', async (_name, create) => {
  const adapter = create();
  const before = await adapter.getProduct('sku-1');
  expect(Number.isSafeInteger(before.price.minor)).toBe(true);
  await expect(adapter.applyPriceChange({
    sku: 'sku-1', price: { minor: 8800, currency: 'CNY' },
    expectedVersion: 'stale', operationId: 'op-1',
  })).rejects.toMatchObject({ code: 'version_conflict' });
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run packages/commerce/src/PlatformAdapter.contract.test.ts`

Expected: FAIL because the commerce package is absent.

- [ ] **Step 3: Implement strict domain types**

Use integer minor units and explicit currency. Define `getProduct`, `getPriceSnapshot`, `queryCampaigns`, `proposePriceChange`, `applyPriceChange`, `getOperationStatus`, `verifyPriceChange`, and `getCapabilities`; normalize errors to `rate_limited`, `timeout`, `server_error`, `auth_expired`, `version_conflict`, `unsupported`, and `unknown_outcome`.

```ts
export type ApplyPriceChangeInput = {
  sku: string;
  price: Money;
  expectedVersion: string;
  operationId: string;
};
```

- [ ] **Step 4: Run type and value tests**

Run: `npx vitest run packages/commerce && npm --workspace @ecomagent/commerce run typecheck`

Expected: PASS for invalid currency, fractional minor amount, overflow, and stale version.

- [ ] **Step 5: Commit**

```powershell
git add packages/commerce
git commit -m "feat: define commerce adapter boundary"
```

### Task 2: Implement three controlled platform protocol adapters

**Files:**
- Create: `packages/platform-simulator/package.json`
- Create: `packages/platform-simulator/src/server.ts`
- Create: `packages/platform-simulator/src/scenarios.ts`
- Create: `packages/platform-simulator/src/protocols/taobao.ts`
- Create: `packages/platform-simulator/src/protocols/jd.ts`
- Create: `packages/platform-simulator/src/protocols/douyin.ts`
- Create: `packages/commerce/src/adapters/TaobaoAdapter.ts`
- Create: `packages/commerce/src/adapters/JdAdapter.ts`
- Create: `packages/commerce/src/adapters/DouyinAdapter.ts`
- Test: `tests/integration/platform-adapters.test.ts`

- [ ] **Step 1: Write failing normalization and dedupe tests**

```ts
it.each(['taobao', 'jd', 'douyin'])('%s normalizes fields and deduplicates operation id', async (platform) => {
  const adapter = adapterFor(platform);
  const first = await adapter.applyPriceChange(change({ operationId: 'op-fixed' }));
  const second = await adapter.applyPriceChange(change({ operationId: 'op-fixed' }));
  expect(second).toEqual(first);
  expect(simulatorWriteCount(platform, 'op-fixed')).toBe(1);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/platform-adapters.test.ts`

Expected: FAIL because no simulator or concrete adapters exist.

- [ ] **Step 3: Implement distinct protocol shapes and fault scripts**

Give each protocol different field names, auth headers, success envelopes, pagination, and error mappings. Support deterministic scripts for rate limit, timeout-before-write, timeout-after-write, 5xx, partial response, auth expiry, and delayed visibility. Persist simulator operations by platform plus `operationId` and expose status lookup.

- [ ] **Step 4: Run the shared adapter suite**

Run: `npx vitest run tests/integration/platform-adapters.test.ts`

Expected: PASS across all three protocols and every normalized error.

- [ ] **Step 5: Commit**

```powershell
git add packages/platform-simulator packages/commerce/src/adapters tests/integration/platform-adapters.test.ts
git commit -m "feat: add three controlled platform adapters"
```

### Task 3: Enforce the single authorized side-effect entry

**Files:**
- Create: `packages/execution/package.json`
- Create: `packages/execution/src/AuthorizationEvidence.ts`
- Create: `packages/execution/src/PriceChangeExecutor.ts`
- Test: `packages/execution/src/PriceChangeExecutor.test.ts`
- Test: `tests/architecture/side-effect-entry.test.ts`

- [ ] **Step 1: Write failing evidence and architecture tests**

```ts
it('rejects approval evidence for another intent', async () => {
  await expect(executor.executeAuthorizedIntent('intent-2', approvalEvidence('intent-1')))
    .rejects.toMatchObject({ code: 'authorization_mismatch' });
});
```

The architecture test must scan TypeScript source and assert that `applyPriceChange(` appears only inside `PriceChangeExecutor.ts`, adapter implementations, and tests.

- [ ] **Step 2: Verify failure**

Run: `npx vitest run packages/execution tests/architecture/side-effect-entry.test.ts`

Expected: FAIL because the executor boundary is missing.

- [ ] **Step 3: Implement evidence validation and final recheck**

```ts
export type AuthorizationEvidence =
  | { kind: 'approval'; approvalId: string; intentId: string; decidedBy: string }
  | { kind: 'policy_allow'; intentId: string; ruleVersion: string; decisionHash: string }
  | { kind: 'benchmark_bypass'; intentId: string; runId: string; environment: 'platform-simulator' };
```

`executeAuthorizedIntent()` must reload the intent, product version, rule version, platform capability, and evidence; allocate a stable operation id; then call the adapter. `benchmark_bypass` must be rejected unless the configured adapter endpoint is the simulator.

- [ ] **Step 4: Verify the boundary**

Run: `npx vitest run packages/execution tests/architecture/side-effect-entry.test.ts`

Expected: PASS for each evidence kind, mismatches, stale state, and forbidden bypass.

- [ ] **Step 5: Commit**

```powershell
git add packages/execution tests/architecture/side-effect-entry.test.ts
git commit -m "feat: enforce authorized price execution"
```

### Task 4: Add leased jobs, fencing, retries, and reconciliation

**Files:**
- Create: `packages/execution/src/ExecutionWorker.ts`
- Create: `packages/execution/src/JobRepository.ts`
- Create: `packages/execution/src/retryPolicy.ts`
- Create: `packages/execution/src/reconcile.ts`
- Test: `tests/integration/execution-reliability.test.ts`

- [ ] **Step 1: Write the failing unknown-outcome and lease tests**

```ts
it('queries status before retrying an unknown write outcome', async () => {
  simulator.script('timeout_after_write');
  await worker.runOne();
  expect(simulator.calls()).toEqual(['apply', 'getOperationStatus']);
  expect(simulator.writeCount()).toBe(1);
});

it('rejects completion from an expired fencing token', async () => {
  const first = jobs.lease('worker-a');
  clock.advanceBy(leaseTtl + 1);
  const second = jobs.lease('worker-b');
  expect(() => jobs.complete(first)).toThrow(/fencing/);
  expect(second.fencingToken).toBeGreaterThan(first.fencingToken);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/execution-reliability.test.ts`

Expected: FAIL because the worker is absent.

- [ ] **Step 3: Implement the job state machine**

Use states `queued`, `leased`, `reconciling`, `succeeded`, `failed`, and `manual_review_required`. Lease with expiry and monotonic fencing token; serialize active jobs by workspace/platform/SKU. Retry rate limits and confirmed pre-write transient failures with capped exponential backoff and jitter. For ambiguous outcomes, query by operation id first; when status lookup is unsupported or still unknown, require manual review and never blind-retry.

- [ ] **Step 4: Run the fault matrix**

Run: `npx vitest run tests/integration/execution-reliability.test.ts`

Expected: PASS for rate limit, 5xx, auth refresh, pre/post-write timeout, stale lease, duplicate delivery, delayed visibility, and unsupported status query; duplicate price changes remain zero.

- [ ] **Step 5: Commit**

```powershell
git add packages/execution/src tests/integration/execution-reliability.test.ts
git commit -m "feat: add reliable execution jobs"
```

### Task 5: Add approval, execution, and replay browser flows

**Files:**
- Modify: `apps/web/src/pages/ApprovalsPage.tsx`
- Modify: `apps/web/src/pages/SessionsPage.tsx`
- Modify: `apps/web/src/pages/ReplayPage.tsx`
- Create: `apps/web/src/components/DecisionTimeline.tsx`
- Create: `apps/server/src/routes/replay.ts`
- Test: `tests/web/approval-replay.spec.ts`

- [ ] **Step 1: Write the failing full-flow test**

```ts
test('approver decides an intent and replay shows the same chain', async ({ page }) => {
  await login(page, 'approver');
  await page.goto('/approvals');
  await page.getByRole('row', { name: /sku-1/ }).getByRole('button', { name: '批准' }).click();
  await expect(page.getByText('执行成功')).toBeVisible();
  await page.goto('/replay/session-1');
  await expect(page.getByText('approval_requested')).toBeVisible();
  await expect(page.getByText('execution_succeeded')).toBeVisible();
});
```

- [ ] **Step 2: Verify failure**

Run: `npm run test:web -- tests/web/approval-replay.spec.ts`

Expected: FAIL because pages are placeholders.

- [ ] **Step 3: Implement real pages and immutable replay**

Show rule reasons, before/proposed price, rule version, actor, attempts, and reconciliation status. Replay reads ordered `agent_events` plus frozen rule/model/tool artifacts and never re-executes side effects. Disable decision buttons after compare-and-set completion.

- [ ] **Step 4: Run browser flow**

Run: `npm run test:web -- tests/web/approval-replay.spec.ts`

Expected: PASS for approve, reject, competing approvers, live status, and replay.

- [ ] **Step 5: Commit**

```powershell
git add apps/web/src/pages apps/web/src/components/DecisionTimeline.tsx apps/server/src/routes/replay.ts tests/web/approval-replay.spec.ts
git commit -m "feat: add approval and replay experience"
```

### Task 6: Implement Skills and Sources end to end

**Files:**
- Create: `packages/skills/package.json`
- Create: `packages/skills/src/SkillRegistry.ts`
- Create: `packages/skills/src/skillSchema.ts`
- Create: `packages/sources/package.json`
- Create: `packages/sources/src/SourceRegistry.ts`
- Create: `apps/server/src/routes/skills.ts`
- Create: `apps/server/src/routes/sources.ts`
- Modify: `apps/web/src/pages/SkillsPage.tsx`
- Modify: `apps/web/src/pages/SourcesPage.tsx`
- Test: `tests/integration/skills-sources.test.ts`
- Test: `tests/web/skills-sources.spec.ts`

- [ ] **Step 1: Write failing acceptance tests**

Create a Skill through the API, enable it for a workspace, attach a simulator Source with encrypted credential fields, run a Pi turn that uses the Skill instructions and Source tool, then assert the secret is absent from events and browser responses.

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/skills-sources.test.ts && npm run test:web -- tests/web/skills-sources.spec.ts`

Expected: FAIL because both registries are missing.

- [ ] **Step 3: Implement registries and management pages**

Version Skills with name, description, instruction Markdown, enabled state, and allowed tools. Define Sources with type, non-secret config, encrypted credentials, health status, and tool factories. Rebuild the workspace tool snapshot only between turns; validate every Source tool through the common registry and PreToolUse Pipeline.

- [ ] **Step 4: Run end-to-end tests**

Run: `npx vitest run tests/integration/skills-sources.test.ts && npm run test:web -- tests/web/skills-sources.spec.ts`

Expected: PASS for create/edit/disable, failed health check, credential rotation, Pi use, and redaction.

- [ ] **Step 5: Commit**

```powershell
git add packages/skills packages/sources apps/server/src/routes/skills.ts apps/server/src/routes/sources.ts apps/web/src/pages/SkillsPage.tsx apps/web/src/pages/SourcesPage.tsx tests/integration/skills-sources.test.ts tests/web/skills-sources.spec.ts
git commit -m "feat: add Skills and Sources management"
```

### Task 7: Implement MCP management and safe tool discovery

**Files:**
- Create: `packages/mcp/package.json`
- Create: `packages/mcp/src/McpManager.ts`
- Create: `packages/mcp/src/toolBridge.ts`
- Create: `apps/server/src/routes/mcp.ts`
- Modify: `apps/web/src/pages/McpPage.tsx`
- Test: `tests/integration/mcp-management.test.ts`
- Test: `tests/web/mcp.spec.ts`

- [ ] **Step 1: Write failing lifecycle tests**

Register a local test MCP server, connect it, discover a read tool and a write-like tool, assert both appear with explicit classifications, run the read tool, and assert the write-like tool receives ASK or DENY from the common safety path.

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/mcp-management.test.ts && npm run test:web -- tests/web/mcp.spec.ts`

Expected: FAIL because MCP management is absent.

- [ ] **Step 3: Implement lifecycle, health, and tool bridging**

Store encrypted environment values, support stdio and HTTP transports, connect/disconnect/test servers, normalize discovered schemas, namespace tools as `mcp.<server>.<tool>`, and require an admin classification of read/propose/write before enabling them. Tool calls must use the shared registry, audit events, timeouts, output-size limits, and redaction.

- [ ] **Step 4: Run integration and browser tests**

Run: `npx vitest run tests/integration/mcp-management.test.ts && npm run test:web -- tests/web/mcp.spec.ts`

Expected: PASS for connection failure, reconnect, schema refresh, classification, invocation, and secret redaction.

- [ ] **Step 5: Commit**

```powershell
git add packages/mcp apps/server/src/routes/mcp.ts apps/web/src/pages/McpPage.tsx tests/integration/mcp-management.test.ts tests/web/mcp.spec.ts
git commit -m "feat: add safe MCP management"
```

### Task 8: Implement durable Automations through the same safety path

**Files:**
- Create: `packages/automations/package.json`
- Create: `packages/automations/src/Scheduler.ts`
- Create: `packages/automations/src/AutomationService.ts`
- Create: `apps/server/src/routes/automations.ts`
- Modify: `apps/web/src/pages/AutomationsPage.tsx`
- Test: `tests/integration/automations.test.ts`
- Test: `tests/web/automations.spec.ts`

- [ ] **Step 1: Write the failing scheduled-run test**

Create a disabled schedule, enable it, advance the fake clock through one due time, and assert exactly one app session is created with the automation actor; any price write still creates an approval.

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/automations.test.ts && npm run test:web -- tests/web/automations.spec.ts`

Expected: FAIL because the scheduler is absent.

- [ ] **Step 3: Implement durable claiming and browser controls**

Store timezone, schedule expression, prompt, workspace, enabled state, next run, and last result. Claim due rows with a lease and unique scheduled occurrence key, create a normal agent session, and run through Pi plus PreToolUse. Provide create/edit/pause/run-now/history actions with admin authorization.

- [ ] **Step 4: Verify scheduling and safety**

Run: `npx vitest run tests/integration/automations.test.ts && npm run test:web -- tests/web/automations.spec.ts`

Expected: PASS for restart, overlapping pollers, timezone boundary, pause, run-now, and ASK enforcement.

- [ ] **Step 5: Commit**

```powershell
git add packages/automations apps/server/src/routes/automations.ts apps/web/src/pages/AutomationsPage.tsx tests/integration/automations.test.ts tests/web/automations.spec.ts
git commit -m "feat: add safe durable automations"
```

**Plan 4 exit gate:** all three controlled adapters, the sole authorized executor, reliability fault matrix, approval/replay UI, Skills, Sources, MCP, and Automations pass end-to-end tests.
