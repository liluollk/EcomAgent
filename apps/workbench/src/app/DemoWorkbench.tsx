import { useState } from "react";
import { ConfirmationCard } from "../features/sessions/ConfirmationCard";
import { Brand, Icon } from "./Icon";

type View = "overview" | "session" | "activity";
type Decision = "pending" | "accept" | "reject";

function ProductArt() {
  return (
    <svg
      className="product-art"
      viewBox="0 0 80 80"
      aria-label="云白陶瓷杯示意图"
      role="img"
    >
      <rect width="80" height="80" rx="16" fill="#eef1f6" />
      <ellipse cx="38" cy="64" rx="22" ry="4" fill="#d8dee9" />
      <path
        d="M51 28h7c14 0 14 22-1 22h-5"
        fill="none"
        stroke="#aebdce"
        strokeWidth="6"
      />
      <path
        d="M20 25h35l-3 30c-2 9-27 9-29 0Z"
        fill="#fff"
        stroke="#c3ceda"
        strokeWidth="1.5"
      />
      <ellipse cx="37.5" cy="25" rx="17.5" ry="5" fill="#d8e1ea" />
      <ellipse cx="37.5" cy="25" rx="13" ry="2.5" fill="#b2c0cc" />
      <path
        d="M27 33v17"
        stroke="#e8edf3"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

const confirmation = {
  id: "demo-confirmation",
  runId: "demo-run",
  summary: "将生活方式旗舰店的云白陶瓷杯调整至 ¥88.00。",
  normalizedArguments: null,
  resourceVersions: { listing: 7 },
  status: "PENDING",
};

export function DemoWorkbench() {
  const [view, setView] = useState<View>("overview");
  const [decision, setDecision] = useState<Decision>("pending");
  const [selectedExample, setSelectedExample] = useState(0);
  const examples = [
    "比较两个店铺的云白陶瓷杯价格，将旗舰店价格调整到 88 元。",
    "查看两个店铺的云白陶瓷杯库存，暂不调整库存。",
    "检查云白陶瓷杯在各店铺的上架状态。",
  ];
  const pending = decision === "pending";
  const accepted = decision === "accept";
  const status = pending ? "待确认" : accepted ? "已完成" : "已拒绝";
  const navItems: Array<{
    view: View;
    label: string;
    icon: "grid" | "chat" | "activity";
  }> = [
    { view: "overview", label: "工作台", icon: "grid" },
    { view: "session", label: "任务会话", icon: "chat" },
    { view: "activity", label: "执行记录", icon: "activity" },
  ];
  const openSession = () => setView("session");

  return (
    <div className="demo-shell">
      <aside className="sidebar">
        <Brand />
        <div className="workspace-switch">
          <span className="workspace-avatar">L</span>
          <span>
            生活方式品牌<small>商品运营工作区</small>
          </span>
          <span className="switch-dots">···</span>
        </div>
        <p className="nav-caption">WORKSPACE</p>
        <nav aria-label="演示导航">
          {navItems.map((item) => (
            <button
              type="button"
              key={item.view}
              aria-current={view === item.view ? "page" : undefined}
              onClick={() => setView(item.view)}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {item.view === "session" && pending && <em>1</em>}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <Icon name="shield" />
          <strong>让每一次操作都有依据</strong>
          <p>
            查询有来源，写入需确认，
            <br />
            执行过程可追溯。
          </p>
          <span>Agent Runtime · v0.1</span>
        </div>
        <div className="sidebar-bottom">
          <span className="user-avatar">L</span>
          <span>
            品牌运营<small>Workspace Owner</small>
          </span>
          <Icon name="settings" size={18} />
        </div>
      </aside>

      <div className="demo-body">
        <header className="topbar">
          <div>
            工作空间 <span>/</span>{" "}
            <strong>
              {navItems.find((item) => item.view === view)?.label}
            </strong>
          </div>
          <div className="topbar-right">
            <span className="demo-badge">
              <i />
              演示工作区
            </span>
            <a href="/">
              进入真实工作台 <Icon name="arrow" size={15} />
            </a>
          </div>
        </header>
        <main className="demo-main">
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                {view === "overview"
                  ? "OPERATIONS OVERVIEW"
                  : view === "session"
                    ? "AGENT WORKSPACE"
                    : "EXECUTION HISTORY"}
              </p>
              <h1>
                {view === "overview"
                  ? "运营工作台"
                  : view === "session"
                    ? "跨店比价与调价"
                    : "每一步，都有记录"}
              </h1>
              <p>
                {view === "overview"
                  ? "把重复的商品运营交给 Agent，把关键决策留给你。"
                  : view === "session"
                    ? "从自然语言到业务操作，始终保留你的控制权。"
                    : "查看示例任务的工具调用、人工决策与执行结果。"}
              </p>
            </div>
            <span className="sample-label">示例数据 · 不连接真实店铺</span>
          </div>

          {view === "overview" ? (
            <>
              <div className="metric-grid">
                <Metric
                  icon="store"
                  label="工作区店铺"
                  value="02"
                  detail="同一工作区，统一协作"
                />
                <Metric
                  icon="chat"
                  label="示例任务"
                  value="03"
                  detail="覆盖查询、比价与调价"
                />
                <Metric
                  icon="shield"
                  label="等待你的确认"
                  value={pending ? "01" : "00"}
                  detail={pending ? "1 项价格变更等待处理" : "当前无待确认操作"}
                  warm
                />
                <Metric
                  icon="activity"
                  label="执行模式"
                  value="人工确认"
                  detail="写操作经确认后执行"
                  text
                />
              </div>
              <div className="overview-columns">
                <div className="primary-column">
                  <section className="panel task-launcher">
                    <div className="section-heading">
                      <div>
                        <span className="agent-icon">
                          <Icon name="bolt" size={18} />
                        </span>
                        <h2>今天，有什么需要处理？</h2>
                      </div>
                      <span className="subtle-label">商品运营 Agent</span>
                    </div>
                    <p>
                      描述你的目标，Agent
                      将查询商品、核对条件，并在写入前请求确认。
                    </p>
                    <div className="example-composer">
                      <p>{examples[selectedExample]}</p>
                      <div>
                        <span>
                          <Icon name="shield" size={14} /> 写操作需确认
                        </span>
                        <button
                          type="button"
                          className="primary-button"
                          onClick={() =>
                            setView(
                              selectedExample === 0 ? "session" : "activity",
                            )
                          }
                        >
                          打开示例 <Icon name="arrow" size={16} />
                        </button>
                      </div>
                    </div>
                    <div className="example-options">
                      {["跨店比价与调价", "库存查询", "上架状态检查"].map(
                        (label, index) => (
                          <button
                            key={label}
                            type="button"
                            aria-pressed={selectedExample === index}
                            onClick={() => setSelectedExample(index)}
                          >
                            <Icon
                              name={
                                index === 0
                                  ? "activity"
                                  : index === 1
                                    ? "box"
                                    : "store"
                              }
                              size={15}
                            />
                            {label}
                          </button>
                        ),
                      )}
                    </div>
                  </section>
                  <section className="panel tasks-panel">
                    <div className="section-heading">
                      <div>
                        <h2>最近任务</h2>
                        <span className="count-label">3</span>
                      </div>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setView("activity")}
                      >
                        查看记录 <Icon name="arrow" size={15} />
                      </button>
                    </div>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>任务</th>
                            <th>业务范围</th>
                            <th>状态</th>
                            <th>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            <td>
                              <strong>云白陶瓷杯 · 跨店比价与调价</strong>
                              <small>SKU-3003 · 价格管理</small>
                            </td>
                            <td>2 家店铺</td>
                            <td>
                              <span
                                className={`status-badge ${pending ? "amber" : accepted ? "green" : "neutral"}`}
                              >
                                {status}
                              </span>
                            </td>
                            <td>
                              <button
                                type="button"
                                className="row-link"
                                aria-label="查看待确认任务"
                                onClick={openSession}
                              >
                                查看 <Icon name="chevron" size={14} />
                              </button>
                            </td>
                          </tr>
                          <tr>
                            <td>
                              <strong>云白陶瓷杯 · 库存查询</strong>
                              <small>SKU-3003 · 库存管理</small>
                            </td>
                            <td>2 家店铺</td>
                            <td>
                              <span className="status-badge green">已完成</span>
                            </td>
                            <td>
                              <button
                                className="row-link"
                                type="button"
                                onClick={() => setView("activity")}
                              >
                                查看 <Icon name="chevron" size={14} />
                              </button>
                            </td>
                          </tr>
                          <tr>
                            <td>
                              <strong>云白陶瓷杯 · 上架状态检查</strong>
                              <small>SKU-3003 · Listing 管理</small>
                            </td>
                            <td>2 家店铺</td>
                            <td>
                              <span className="status-badge green">已完成</span>
                            </td>
                            <td>
                              <button
                                className="row-link"
                                type="button"
                                onClick={() => setView("activity")}
                              >
                                查看 <Icon name="chevron" size={14} />
                              </button>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <div className="panel-footnote">
                      <span className="tiny-dot" />
                      所有示例操作仅保存在当前页面，刷新后重置。
                    </div>
                  </section>
                  <section className="workflow-strip">
                    <span className="workflow-label">一次受控的执行</span>
                    {["查询商品", "跨店比价", "人工确认", "写后回读"].map(
                      (label, index) => (
                        <div key={label}>
                          <span className={index === 2 ? "active-step" : ""}>
                            {index + 1}
                          </span>
                          {label}
                          {index < 3 && <Icon name="chevron" size={14} />}
                        </div>
                      ),
                    )}
                  </section>
                </div>
                <aside className="secondary-column">
                  <StorePanel />
                  <section className="panel guard-panel">
                    <span className="guard-icon">
                      <Icon name="shield" size={24} />
                    </span>
                    <h2>执行有边界，操作有保障</h2>
                    <p>模型提出操作建议，Runtime 负责检查执行条件。</p>
                    <ul>
                      <li>
                        <Icon name="check" size={16} />
                        店铺与动作权限校验
                      </li>
                      <li>
                        <Icon name="check" size={16} />
                        价格策略与资源版本检查
                      </li>
                      <li>
                        <Icon name="check" size={16} />
                        关键写操作人工确认
                      </li>
                      <li>
                        <Icon name="check" size={16} />
                        不确定结果回查与恢复
                      </li>
                    </ul>
                    <span className="guard-footer">
                      CONFIRM_WRITES <Icon name="shield" size={14} />
                    </span>
                  </section>
                </aside>
              </div>
            </>
          ) : view === "session" ? (
            <div className="session-columns">
              <section className="panel conversation-panel">
                <div className="conversation-heading">
                  <span className="agent-icon">
                    <Icon name="bolt" size={19} />
                  </span>
                  <div>
                    <h2>商品运营 Agent</h2>
                    <p>价格管理 · SKU-3003</p>
                  </div>
                  <span
                    className={`status-badge ${pending ? "amber" : accepted ? "green" : "neutral"}`}
                  >
                    {status}
                  </span>
                </div>
                <div className="conversation-content">
                  <div className="user-message">
                    <span className="user-avatar">L</span>
                    <p>{examples[0]}</p>
                  </div>
                  <div className="agent-response">
                    <span className="response-label">
                      <Icon name="bolt" size={16} /> EcomAgent
                    </span>
                    <p>
                      已找到两个店铺中的同款商品。旗舰店当前售价高于精选店，以下是比价结果和拟执行的调整。
                    </p>
                    <div className="product-summary">
                      <ProductArt />
                      <div>
                        <strong>云白陶瓷杯</strong>
                        <span>350 mL / 云白色</span>
                        <code>SKU-3003</code>
                      </div>
                      <span className="subtle-label">同款商品 · 2 家店铺</span>
                    </div>
                    <div className="price-comparison">
                      <div>
                        <span>
                          <i className="store-letter orange">旗</i>
                          生活方式旗舰店
                        </span>
                        <strong>
                          ¥99<small>.00</small>
                        </strong>
                        <span className="subtle-label">调整前价格</span>
                      </div>
                      <div>
                        <span>
                          <i className="store-letter blue">精</i>生活方式精选店
                        </span>
                        <strong>
                          ¥89<small>.00</small>
                        </strong>
                        <span className="subtle-label">参考价格</span>
                      </div>
                    </div>
                    <div
                      className={`price-proposal ${pending ? "" : "resolved"}`}
                    >
                      <div className="proposal-title">
                        <Icon name="shield" size={18} />
                        <strong>
                          {pending
                            ? "这项改动，需要你的确认"
                            : accepted
                              ? "示例操作已完成"
                              : "本次操作已拒绝"}
                        </strong>
                        <span className="subtle-label">仅影响旗舰店</span>
                      </div>
                      <div className="price-change">
                        <div>
                          <span>当前价格</span>
                          <strong>¥99.00</strong>
                        </div>
                        <Icon name="arrow" size={24} />
                        <div>
                          <span>目标价格</span>
                          <strong>¥88.00</strong>
                        </div>
                        <span className="price-delta">
                          − ¥11.00 <small>降幅 11.11%</small>
                        </span>
                      </div>
                      {pending ? (
                        <ConfirmationCard
                          request={confirmation}
                          onDecide={(_, nextDecision) =>
                            setDecision(nextDecision)
                          }
                        />
                      ) : (
                        <div className="decision-result">
                          <p role="status">
                            {accepted
                              ? "示例改价已完成，当前价格 ¥88.00"
                              : "已拒绝本次改价，示例价格保持 ¥99.00"}
                          </p>
                          <button
                            type="button"
                            onClick={() => setDecision("pending")}
                          >
                            重新演示
                          </button>
                        </div>
                      )}
                    </div>
                    <p className="message-note">
                      <Icon name="shield" size={14} />
                      演示仅更新本页状态；真实执行会重新校验权限、规则与资源版本。
                    </p>
                  </div>
                </div>
                <div className="session-bottom">
                  <Icon name="chat" size={18} />
                  <span>
                    {pending
                      ? "处理当前确认后，即可继续下一步。"
                      : "此任务的示例流程已结束，可重新演示或查看执行记录。"}
                  </span>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setView("activity")}
                  >
                    执行记录 <Icon name="arrow" size={15} />
                  </button>
                </div>
              </section>
              <aside className="secondary-column">
                <section className="panel execution-panel">
                  <div className="section-heading">
                    <h2>执行轨迹</h2>
                    <span className="subtle-label">示例</span>
                  </div>
                  <p className="trace-intro">每个业务动作，都有迹可循。</p>
                  <ol className="execution-timeline">
                    <li className="done">
                      <span className="step-node">
                        <Icon name="check" size={13} />
                      </span>
                      <strong>查询商品</strong>
                      <p>匹配 SKU 与授权店铺</p>
                      <code>search_products</code>
                    </li>
                    <li className="done">
                      <span className="step-node">
                        <Icon name="check" size={13} />
                      </span>
                      <strong>跨店比价</strong>
                      <p>读取两个店铺的当前售价</p>
                      <code>compare_listing_prices</code>
                    </li>
                    <li className={pending ? "current" : "done"}>
                      <span className="step-node">
                        {pending ? "3" : <Icon name="check" size={13} />}
                      </span>
                      <strong>
                        {pending
                          ? "等待人工确认"
                          : accepted
                            ? "已确认价格调整"
                            : "用户拒绝操作"}
                      </strong>
                      <p>
                        {pending
                          ? "执行暂停，等待你的决定"
                          : "已记录当前示例决策"}
                      </p>
                      <code>
                        {pending
                          ? "WAITING_CONFIRMATION"
                          : accepted
                            ? "CONFIRMED"
                            : "REJECTED"}
                      </code>
                    </li>
                    <li className={accepted ? "done" : "upcoming"}>
                      <span className="step-node">
                        {accepted ? <Icon name="check" size={13} /> : "4"}
                      </span>
                      <strong>
                        {decision === "reject"
                          ? "跳过价格写入"
                          : "执行与结果回读"}
                      </strong>
                      <p>
                        {accepted
                          ? "示例价格已更新为 ¥88.00"
                          : decision === "reject"
                            ? "价格保持不变"
                            : "确认后写入，并读取最新状态"}
                      </p>
                    <code>set_listing_price</code>
                    </li>
                  </ol>
                  <div className="trace-footer">
                    <Icon name="shield" size={17} />
                    <span>
                      写操作只作用于
                      <br />
                      <strong>当前授权的目标店铺</strong>
                    </span>
                  </div>
                </section>
                <div className="demo-explainer">
                  <strong>可交互的本地演示</strong>
                  <p>
                    你可以确认或拒绝此次改价。这里没有真实模型请求，也不会产生真实店铺写入。
                  </p>
                </div>
              </aside>
            </div>
          ) : (
            <section className="panel activity-panel">
              <div className="section-heading">
                <div>
                  <h2>操作记录</h2>
                  <span className="count-label">3</span>
                </div>
                <span className="subtle-label">当前演示会话</span>
              </div>
              <div className="activity-entry">
                <span className="activity-symbol">
                  <Icon name="activity" />
                </span>
                <div>
                  <h3>跨店比价与调价</h3>
                  <p>旗舰店 · 云白陶瓷杯 · ¥99.00 → ¥88.00</p>
                  <code>
                    {pending
                      ? "WAITING_CONFIRMATION"
                      : accepted
                        ? "COMPLETED"
                        : "REJECTED"}
                  </code>
                </div>
                <span
                  className={`status-badge ${pending ? "amber" : accepted ? "green" : "neutral"}`}
                >
                  {status}
                </span>
                <button
                  type="button"
                  className="text-button"
                  onClick={openSession}
                >
                  查看详情 <Icon name="arrow" size={15} />
                </button>
              </div>
              <div className="activity-entry">
                <span className="activity-symbol">
                  <Icon name="box" />
                </span>
                <div>
                  <h3>库存查询</h3>
                  <p>旗舰店 128 件 · 精选店 86 件 · 仅查询，未修改库存</p>
                  <code>get_inventory_level</code>
                </div>
                <span className="status-badge green">已完成</span>
              </div>
              <div className="activity-entry">
                <span className="activity-symbol">
                  <Icon name="store" />
                </span>
                <div>
                  <h3>上架状态检查</h3>
                  <p>旗舰店：已上架 · 精选店：已上架 · 未修改发布状态</p>
                  <code>get_listing</code>
                </div>
                <span className="status-badge green">已完成</span>
              </div>
              <div className="panel-footnote">
                本页为预置示例及当前页面交互结果，不代表真实后端审计记录。
              </div>
            </section>
          )}
          <footer className="page-footer">
            <span>
              EcomAgent <span>/</span> 让 AI 参与运营，让执行保持可控。
            </span>
            <span>查询 · 确认 · 执行 · 回查</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

function Metric({
  icon,
  label,
  value,
  detail,
  warm = false,
  text = false,
}: {
  icon: "store" | "chat" | "shield" | "activity";
  label: string;
  value: string;
  detail: string;
  warm?: boolean;
  text?: boolean;
}) {
  return (
    <section className={`metric ${warm ? "warm" : ""}`}>
      <div>
        <span>{label}</span>
        <Icon name={icon} size={18} />
      </div>
      <strong className={text ? "text-metric" : ""}>{value}</strong>
      <p>{detail}</p>
    </section>
  );
}

function StorePanel() {
  return (
    <section className="panel stores-panel">
      <div className="section-heading">
        <h2>工作区店铺</h2>
        <span className="count-label">2</span>
      </div>
      <p>跨店铺协作，独立授权范围。</p>
      {["生活方式旗舰店", "生活方式精选店"].map((store, index) => (
        <div className="store-row" key={store}>
          <span className={`store-letter ${index === 0 ? "orange" : "blue"}`}>
            {index === 0 ? "旗" : "精"}
          </span>
          <div>
            <strong>{store}</strong>
            <small>Mock Commerce · 示例店铺</small>
          </div>
          <span className="tiny-dot" />
        </div>
      ))}
      <div className="store-bottom">
        <Icon name="box" size={15} />
        <span>商品、定价、库存与 Listing</span>
      </div>
    </section>
  );
}
