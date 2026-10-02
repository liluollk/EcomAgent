# 项目架构图 HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建一张可交互、可截图、与已确认设计一致的单文件 Agent Runtime 项目架构图。

**Architecture:** 使用语义化 HTML、CSS Grid 和少量原生 JavaScript 构造分层蓝图；系统架构与 Run 执行链共享同一组模块，通过状态类切换高亮。所有内容内嵌，不依赖外部资源。

**Tech Stack:** HTML5, CSS Grid, inline SVG, vanilla JavaScript, PowerShell, Chromium headless

---

### Task 1: 构建自包含架构图

**Files:**
- Create: `docs/架构/项目架构图.html`
- Reference: `docs/架构/项目架构图设计.md`
- Reference: `docs/需求/Agent工作台与Runtime需求文档.md`

- [x] **Step 1: 运行结构检查确认目标文件尚不存在**

```powershell
$diagram = 'docs/架构/项目架构图.html'
if (Test-Path -LiteralPath $diagram) {
    throw "Expected architecture diagram to be absent before implementation"
}
```

Expected: 命令退出码为 0。

- [x] **Step 2: 创建语义化单文件 HTML**

HTML 必须包含 `header/main/aside/footer`、视图切换按钮、Runtime 总框、五层结构、独立 Eval 旁路、模块说明区和 Run 步骤轨道。每个可选择模块使用唯一 `data-node`，连接关系使用 `data-connects` 声明。

- [x] **Step 3: 实现视觉与响应式布局**

使用设计文档中的六色 Token、系统字体与工程蓝图式结构。桌面端为主图加右侧说明栏；小于 `900px` 时改为纵向；小于 `560px` 时模块网格改单列。所有焦点态可见，并支持 `prefers-reduced-motion`。

- [x] **Step 4: 实现视图与模块交互**

```javascript
function setView(view) {
  document.body.dataset.view = view
  document.querySelectorAll('[data-view-button]').forEach((button) => {
    const active = button.dataset.viewButton === view
    button.setAttribute('aria-pressed', String(active))
  })
}

function selectNode(nodeId) {
  const node = document.querySelector(`[data-node="${nodeId}"]`)
  const connected = new Set((node?.dataset.connects || '').split(' ').filter(Boolean))
  connected.add(nodeId)
  document.querySelectorAll('[data-node]').forEach((candidate) => {
    candidate.classList.toggle('is-related', connected.has(candidate.dataset.node))
  })
}
```

模块选择还必须更新说明区；Escape 或“重置”按钮清除选择。Enter/Space 与点击具有相同行为。

- [x] **Step 5: 运行结构和外部依赖检查**

```powershell
$html = Get-Content -LiteralPath 'docs/架构/项目架构图.html' -Raw
foreach ($required in @(
    'Runtime Control Plane',
    'Runtime Execution Plane',
    'Agent Harness',
    'Capability Layer',
    'PostgreSQL',
    'ToolResultProcessor',
    'EventReducer',
    'Eval'
)) {
    if (-not $html.Contains($required)) { throw "Missing architecture element: $required" }
}
if ($html -match '<script[^>]+src=' -or $html -match '<link[^>]+href=') {
    throw 'External script or stylesheet is not allowed'
}
```

Expected: 命令退出码为 0。

- [x] **Step 6: 使用 Chromium 截图检查桌面布局**

```powershell
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$htmlPath = (Resolve-Path 'docs/架构/项目架构图.html').Path.Replace('\', '/')
& $edge --headless --disable-gpu --hide-scrollbars --window-size=1440,900 --screenshot='architecture-diagram.png' "file:///$htmlPath"
```

Expected: 生成 1440×900 截图；主图无横向滚动、遮挡、截断或文字重叠。

- [x] **Step 7: 提交**

```powershell
git add docs/架构/项目架构图.html docs/架构/项目架构图设计.md docs/计划/项目架构图HTML实施计划.md
git commit -m "docs: add interactive runtime architecture diagram"
```
