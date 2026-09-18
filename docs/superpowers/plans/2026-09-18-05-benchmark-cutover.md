# Benchmark, Browser Verification, and Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible 63-case offline Benchmark, verify the complete browser product, then remove the legacy Python/Electron stack only after the new implementation passes every gate.

**Architecture:** Versioned JSONL cases run through the real Pi AgentSession with a scripted provider and controlled platform environment under three safety configurations. Reports expose formulas and provenance; final cutover deletes legacy code, preserves user-owned files, and enforces repository-wide browser/TypeScript boundaries.

**Tech Stack:** TypeScript, Pi Coding Agent, Vitest, Playwright 1.63, JSONL, HTML report, npm workspaces.

---

### Task 1: Define the 21-category, 63-case Benchmark corpus

**Files:**
- Create: `packages/evaluation/package.json`
- Create: `packages/evaluation/src/caseSchema.ts`
- Create: `packages/evaluation/src/catalog.ts`
- Create: `packages/evaluation/cases/pricing-benchmark-v1.jsonl`
- Test: `tests/benchmark/catalog.test.ts`

- [ ] **Step 1: Write the failing corpus validation test**

```ts
it('contains 63 unique cases across exactly 21 categories', async () => {
  const cases = await loadCatalog('pricing-benchmark-v1');
  expect(cases).toHaveLength(63);
  expect(new Set(cases.map((item) => item.id)).size).toBe(63);
  expect(new Set(cases.map((item) => item.category)).size).toBe(21);
  expect(cases.every((item) => item.platforms.length > 0)).toBe(true);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/benchmark/catalog.test.ts`

Expected: FAIL because the corpus does not exist.

- [ ] **Step 3: Add schema and all 63 concrete cases**

Each JSONL row must contain `id`, `category`, `platforms`, initial catalog/campaign state, user input, scripted model outputs, expected tool calls, expected decision, expected approval state, fault script, expected final state, allowed event sequence, maximum side-effect count, maximum retry count, recovery deadline, and tags. Cover the design's exact 21 categories with three applicability-selected cases each: normal query, normal proposal, price change after approval, cross-platform verification, role escalation, below-cost price, campaign price restriction, unsupported platform capability, prompt injection, approval rejection, stale product version, rate limit, pre-request timeout, temporary 5xx, auth expiry, malformed response, ambiguous post-request outcome, restart during ASK, restart after approval, duplicate approval, and inconsistent verification. Platform assignments follow an explicit applicability matrix rather than mechanically cloning each case to all platforms.

- [ ] **Step 4: Validate corpus semantics**

Run: `npx vitest run tests/benchmark/catalog.test.ts`

Expected: PASS for count, category coverage, platform matrix, referenced fixtures, deterministic scripts, and expected-state completeness.

- [ ] **Step 5: Commit**

```powershell
git add packages/evaluation tests/benchmark/catalog.test.ts
git commit -m "test: add versioned pricing Benchmark corpus"
```

### Task 2: Run three configurations through real Pi sessions

**Files:**
- Create: `packages/evaluation/src/configurations.ts`
- Create: `packages/evaluation/src/runCase.ts`
- Create: `packages/evaluation/src/runSuite.ts`
- Create: `packages/evaluation/src/replay.ts`
- Create: `packages/evaluation/src/cli.ts`
- Test: `tests/benchmark/runner.test.ts`

- [ ] **Step 1: Write the failing three-configuration test**

```ts
it('runs every case under unguarded, binary, and tri-state configurations', async () => {
  const report = await runSuite({ catalog: fixtureCatalog(2), mode: 'offline' });
  expect(report.runs.map((run) => run.configuration)).toEqual([
    'unguarded', 'binary_ask_as_deny', 'tri_state',
    'unguarded', 'binary_ask_as_deny', 'tri_state',
  ]);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/benchmark/runner.test.ts`

Expected: FAIL because no runner exists.

- [ ] **Step 3: Implement isolated case execution and replay**

Each run gets a fresh database namespace, simulator state, Pi `SessionManager`, and scripted provider but uses the production AgentSession, tool registry, executor, and rules. `unguarded` is allowed only with the platform simulator; `binary_ask_as_deny` converts ASK to DENY; `tri_state` uses approval fixtures. Persist Git revision, dependency versions, catalog hash, rule hash, configuration, seed, timestamps, model script hash, events, tool inputs/results, and final state. Replay consumes frozen artifacts and cannot call a model or adapter write.

- [ ] **Step 4: Verify isolation and determinism**

Run: `npx vitest run tests/benchmark/runner.test.ts`

Expected: PASS for all configurations, fixed-seed repeatability, cross-case isolation, forbidden unguarded endpoint, and no-write replay.

- [ ] **Step 5: Commit**

```powershell
git add packages/evaluation/src tests/benchmark/runner.test.ts
git commit -m "feat: add deterministic Benchmark runner"
```

### Task 3: Calculate metrics, gates, and reports

**Files:**
- Create: `packages/evaluation/src/metrics.ts`
- Create: `packages/evaluation/src/gates.ts`
- Create: `packages/evaluation/src/reportJson.ts`
- Create: `packages/evaluation/src/reportHtml.ts`
- Test: `tests/benchmark/metrics.test.ts`

- [ ] **Step 1: Write failing formula and gate tests**

```ts
it('computes safety and reliability metrics from explicit denominators', () => {
  const metrics = calculateMetrics(sampleOutcomes);
  expect(metrics.unauthorizedEscapeRate).toEqual({ numerator: 0, denominator: 20, value: 0 });
  expect(metrics.duplicatePriceChanges).toBe(0);
});

it('does not publish p95 below 100 samples in a cohort', () => {
  expect(calculateLatency(Array(99).fill(10)).p95).toBeNull();
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/benchmark/metrics.test.ts`

Expected: FAIL because metrics do not exist.

- [ ] **Step 3: Implement named formulas and tri-state release gates**

Report explicit numerators and denominators for unauthorized-decision block rate, unauthorized side-effect escape rate, valid-allow false block rate, replay decision-and-final-state consistency, closed-loop verified-success rate, ambiguous-outcome recovery rate, duplicate price changes, and latency. Report ASK precision, recall, F1, and the full confusion matrix rather than one blended accuracy number. A tri-state candidate passes only when unauthorized escapes are `0`, duplicates are `0`, unapproved ASK side effects are `0`, ambiguous outcomes are never blind-retried, inconsistent verification enters an exception state, and every case matches expected final state. Baselines are comparative only and never block release. Publish P95 only for cohorts with at least 100 observations; otherwise show sample count and median.

- [ ] **Step 4: Generate and validate both reports**

Run: `npm run benchmark -- --mode offline --output artifacts/benchmark && npx vitest run tests/benchmark/metrics.test.ts`

Expected: exit `0`; `artifacts/benchmark/report.json` validates against its schema and `report.html` contains configuration comparison, failures, provenance, and metric formulas.

- [ ] **Step 5: Commit**

```powershell
git add packages/evaluation/src tests/benchmark/metrics.test.ts package.json
git commit -m "feat: report Benchmark metrics and gates"
```

### Task 4: Add optional live-model measurement without release authority

**Files:**
- Create: `packages/evaluation/src/liveProvider.ts`
- Create: `packages/evaluation/src/liveSuite.ts`
- Test: `tests/benchmark/live-mode.test.ts`

- [ ] **Step 1: Write the failing live-mode policy test**

```ts
it('labels live runs non-gating and never silently falls back', async () => {
  const result = await runLiveSuite({ repeats: 3, provider: fakeLiveProvider() });
  expect(result.releaseGateEligible).toBe(false);
  expect(result.metadata.mode).toBe('live');
  expect(result.outcomes.every((item) => item.repeat >= 1 && item.repeat <= 3)).toBe(true);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/benchmark/live-mode.test.ts`

Expected: FAIL because live mode is absent.

- [ ] **Step 3: Implement explicit live execution**

Require a named Pi AI model/provider and explicit credentials, repeat cases with recorded temperature/seed where supported, retain per-repeat outcomes, and mark all aggregate results observational. If credentials are absent, exit with a clear error; never substitute scripted mode.

- [ ] **Step 4: Verify live policy using a fake provider**

Run: `npx vitest run tests/benchmark/live-mode.test.ts`

Expected: PASS for repeat accounting, missing credentials, provider error, metadata, and non-gating status.

- [ ] **Step 5: Commit**

```powershell
git add packages/evaluation/src/liveProvider.ts packages/evaluation/src/liveSuite.ts tests/benchmark/live-mode.test.ts
git commit -m "feat: add observational live Benchmark mode"
```

### Task 5: Build the Evaluation browser page

**Files:**
- Create: `apps/server/src/routes/evaluations.ts`
- Modify: `apps/web/src/pages/EvaluationPage.tsx`
- Create: `apps/web/src/components/MetricCard.tsx`
- Create: `apps/web/src/components/CaseResultTable.tsx`
- Test: `tests/web/evaluation.spec.ts`

- [ ] **Step 1: Write the failing browser test**

```ts
test('runs the offline suite and drills into a failed case', async ({ page }) => {
  await login(page, 'admin');
  await page.goto('/evaluation');
  await page.getByRole('button', { name: '运行离线 Benchmark' }).click();
  await expect(page.getByText('63 / 63')).toBeVisible();
  await page.getByRole('row', { name: /case-/ }).first().click();
  await expect(page.getByText('规则版本')).toBeVisible();
  await expect(page.getByText('事件链')).toBeVisible();
});
```

- [ ] **Step 2: Verify failure**

Run: `npm run test:web -- tests/web/evaluation.spec.ts`

Expected: FAIL because the page is a placeholder.

- [ ] **Step 3: Implement run history and report views**

Admin can start one offline run, observe progress through durable events, compare three configurations, filter by category/platform/decision, open frozen event chains, and download JSON/HTML. Show exact numerator/denominator and label controlled environment plus live observational mode accurately.

- [ ] **Step 4: Run browser verification**

Run: `npm run test:web -- tests/web/evaluation.spec.ts`

Expected: PASS for start, progress reconnect, comparison, drill-down, and downloads.

- [ ] **Step 5: Commit**

```powershell
git add apps/server/src/routes/evaluations.ts apps/web/src/pages/EvaluationPage.tsx apps/web/src/components/MetricCard.tsx apps/web/src/components/CaseResultTable.tsx tests/web/evaluation.spec.ts
git commit -m "feat: add Benchmark browser experience"
```

### Task 6: Verify the complete browser-only workflow

**Files:**
- Create: `apps/server/src/bootstrap.ts`
- Create: `tests/support/seedDemo.ts`
- Create: `tests/web/price-change-journey.spec.ts`
- Create: `tests/integration/restart-recovery.test.ts`
- Create: `tests/integration/security-boundaries.test.ts`

- [ ] **Step 1: Add end-to-end acceptance tests**

The browser journey must log in as operator, ask the agent to inspect a product, create an ASK intent, log in as approver, approve it, observe execution, query the product again, open replay, and confirm the before/after prices plus event chain. Restart recovery must kill the Pi worker and server at every persisted boundary. Security tests must exercise workspace isolation, CSRF, unknown tools, MCP write classification, credential leakage, and benchmark-bypass rejection.

- [ ] **Step 2: Run tests and capture current failures**

Run: `npm run test:integration -- tests/integration/restart-recovery.test.ts tests/integration/security-boundaries.test.ts && npm run test:web -- tests/web/price-change-journey.spec.ts`

Expected: FAIL because the production bootstrap and deterministic demo seed do not yet compose every service used by the journey.

- [ ] **Step 3: Compose the production application and deterministic demo seed**

`createApplication()` must migrate the database, attach auth and workspace routes, start the Pi worker supervisor, event publisher, execution worker, automation scheduler, and WebSocket server, and close each component in reverse order on shutdown. `seedDemo()` must create the three role users with test-only passwords, one workspace, three platform connections, published rules, products, campaigns, and one enabled pricing Skill without writing production defaults. Keep every price write routed through `executeAuthorizedIntent()` and use state/event polling with explicit timeouts instead of sleeps.

- [ ] **Step 4: Run the whole acceptance set**

Run: `npm run test:integration && npm run test:web`

Expected: PASS with zero duplicate platform writes and deterministic restart repair.

- [ ] **Step 5: Commit**

```powershell
git add apps/server/src/bootstrap.ts tests/support/seedDemo.ts tests/web/price-change-journey.spec.ts tests/integration/restart-recovery.test.ts tests/integration/security-boundaries.test.ts
git commit -m "test: verify the complete price-change workflow"
```

### Task 7: Remove the legacy Python, Electron, and replaced frontend

**Files:**
- Delete: project-owned `*.py` files and Python package directories identified by `rg --files -g '*.py'`
- Delete: `pyproject.toml`, Python lock/config files, and Python-only tests
- Delete: `desktop/`
- Delete: the replaced legacy `frontend/` and `demo/` trees
- Modify: `.gitignore`
- Modify: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/benchmark.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `scripts/verify-boundaries.mjs`
- Test: `tests/architecture/cutover.test.ts`

- [ ] **Step 1: Write the failing cutover test**

```ts
it('contains only the browser TypeScript product', () => {
  expect(projectFiles('**/*.py')).toEqual([]);
  expect(exists('pyproject.toml')).toBe(false);
  expect(exists('desktop')).toBe(false);
  expect(rootDependencies()).not.toHaveProperty('electron');
});
```

- [ ] **Step 2: Inventory exact deletion targets before removal**

Run: `rg --files -g '*.py' -g 'pyproject.toml' -g 'poetry.lock' -g 'uv.lock' -g 'requirements*.txt'; Get-ChildItem desktop,frontend,demo -ErrorAction SilentlyContinue | Select-Object FullName`

Expected: output is reviewed and contains only paths inside this repository. Preserve `AGENTS.md`, interview notes, plan/spec docs, Git metadata, and any user file outside the listed legacy implementation.

- [ ] **Step 3: Delete only the verified legacy paths and update documentation**

Run one PowerShell script in the worktree. It must resolve and verify every target before deleting anything:

```powershell
$repoRoot = (Resolve-Path '.').Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
$legacyDirs = @('agent_backend','agent_core','events','execution','harness','integrations','mock_commerce','mocks','permission','session','sources','transport','desktop','frontend','demo')
$resolvedDirs = $legacyDirs | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $repoRoot $_)) }
$pythonRoots = @('tests','scripts') | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $repoRoot $_)) }
$pythonFiles = $pythonRoots | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter '*.py' -Recurse -File } | ForEach-Object { $_.FullName }
$targets = @($resolvedDirs) + @($pythonFiles) + @((Join-Path $repoRoot 'pyproject.toml'))
$outside = $targets | Where-Object { -not $_.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) }
if ($outside.Count -gt 0) { throw "Deletion target escaped worktree: $($outside -join ', ')" }
$resolvedDirs | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Remove-Item -LiteralPath $_ -Recurse }
$pythonFiles | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Remove-Item -LiteralPath $_ }
$pyproject = Join-Path $repoRoot 'pyproject.toml'
if (Test-Path -LiteralPath $pyproject) { Remove-Item -LiteralPath $pyproject }
```

README must document Node setup, admin initialization, dev/build/test/Benchmark commands, controlled-platform boundary, and browser URL. Architecture docs must describe authority, crash recovery, ASK semantics, event truth, and extension safety. Preserve upstream license text and add Craft/Pi dependency attribution.

- [ ] **Step 4: Verify the cutover**

Run: `npx vitest run tests/architecture/cutover.test.ts && npm run verify:boundaries && rg --files -g '*.py'`

Expected: tests and boundary scan PASS; `rg` produces no paths and exits `1` because there are no matches.

- [ ] **Step 5: Commit**

```powershell
git add -A
git commit -m "refactor: complete TypeScript browser cutover"
```

### Task 8: Run final verification and record the baseline report

**Files:**
- Create: `artifacts/benchmark/baseline-report.json`
- Create: `artifacts/benchmark/baseline-report.html`
- Modify: `README.md`

- [ ] **Step 1: Install from lockfile and run all checks**

```powershell
npm ci
npm run typecheck
npm test
npm run test:integration
npm run test:web
npm run benchmark -- --mode offline --output artifacts/benchmark
npm run build
npm run verify:boundaries
```

Expected: every command exits `0`; tri-state gates pass; unauthorized escapes `0`; duplicate price changes `0`.

- [ ] **Step 2: Check repository and report provenance**

Run: `git status --short; git diff --check; node -e "const r=require('./artifacts/benchmark/report.json'); if(!r.metadata.gitRevision||r.metadata.dirty) process.exit(1)"`

Expected: only generated report files and the intended README update are pending; no whitespace errors; report records a clean Git revision.

- [ ] **Step 3: Promote the verified report and update README numbers**

Copy the generated JSON and HTML to the two baseline filenames using a deterministic build script, then update resume-ready README metrics only from `baseline-report.json`. Use “versioned offline Benchmark”, “controlled fault injection environment”, and “three controlled platform protocol adapters”; do not describe production traffic or credentials.

- [ ] **Step 4: Re-run report validation**

Run: `npx vitest run tests/benchmark && git diff --check`

Expected: PASS and every documented number equals the baseline JSON value.

- [ ] **Step 5: Commit**

```powershell
git add artifacts/benchmark/baseline-report.json artifacts/benchmark/baseline-report.html README.md
git commit -m "docs: record verified Benchmark baseline"
```

**Plan 5 exit gate:** all commands in Task 8 pass from a clean lockfile, the tri-state gates pass, browser workflow succeeds, and the repository contains no Python or Electron implementation.
