import React, { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDown,
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Box,
  Braces,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Code2,
  Copy,
  Cpu,
  Database,
  ExternalLink,
  FileCode2,
  FlaskConical,
  GitBranch,
  Layers3,
  LockKeyhole,
  Menu,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  Upload,
  Users,
  X,
} from "lucide-react";
import "./style.css";
import Forum from "./Forum";
import Hero from "./Hero";
import ResearchVisual from "./ResearchVisual";
import "./design.css";
import "./polish.css";
import "./research-visual.css";

const templates = [
  {
    id: "lgbm",
    title: "LightGBM · 截面排序",
    owner: "WiseHold Research",
    label: "参考实验",
    model: "LightGBM",
    task: "A 股 · 5 日预测",
    color: "mint",
    description: "多种子集成与滚动训练，从基准模型开始建立研究证据。",
    metric: "+0.1104",
    metricLabel: "样本外 Rank IC",
    secondary: "+2.7%",
    secondaryLabel: "扣成本年化净超额",
    tags: ["3 seeds", "Walk-forward", "2024–2025"],
    icon: Layers3,
  },
  {
    id: "kronos",
    title: "Kronos · 时序微调",
    owner: "WiseHold Templates",
    label: "训练模板",
    model: "Kronos",
    task: "时序基础模型",
    color: "lavender",
    description: "从预训练时序模型出发，探索适合自己研究任务的微调方法。",
    tags: ["Fine-tuning", "时序模型", "本地训练"],
    icon: Activity,
  },
  {
    id: "custom",
    title: "你的模型 · 你的方法",
    owner: "Community",
    label: "开放分享",
    model: "自定义",
    task: "研究者共建",
    color: "peach",
    description: "分享模型卡、训练配置与结果，让其他研究者理解并复现你的工作。",
    tags: ["Model card", "Recipe", "Hugging Face"],
    icon: Braces,
  },
];

function Logo({ light = false }) {
  return (
    <a
      className={`brand ${light ? "brand-light" : ""}`}
      href="/"
      aria-label="WiseHold 首页"
    >
      <span className="brand-mark">
        <Layers3 size={22} strokeWidth={2} />
      </span>
      <span>
        WiseHold
      </span>
    </a>
  );
}

function Modal({ title, subtitle, children, close, wide = false }) {
  const panel = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    const key = (e) => {
      if (e.key === "Escape") close();
      if (e.key === "Tab") {
        const all = panel.current.querySelectorAll(
          'a[href],button,input,textarea,select,[tabindex="0"]',
        );
        const first = all[0],
          last = all[all.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.body.style.overflow = "";
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [close]);
  return (
    <div
      className="modal-backdrop"
      onClick={(e) => e.target === e.currentTarget && close()}
    >
      <section
        className={`modal ${wide ? "modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        tabIndex={-1}
        ref={panel}
      >
        <button
          className="icon-button modal-close"
          title="关闭"
          aria-label="关闭"
          onClick={close}
        >
          <X size={21} />
        </button>
        <div className="eyebrow">WISEHOLD</div>
        <h2 id="dialog-title">{title}</h2>
        {subtitle && <p className="modal-subtitle">{subtitle}</p>}
        {children}
      </section>
    </div>
  );
}

function App() {
  const reducedMotion = useReducedMotion();
  const [site, setSite] = useState({
    user: null,
    models: [],
    csrf: "",
    dev: false,
  });
  const [connectionError, setConnectionError] = useState(false);
  const [modal, setModal] = useState(new URLSearchParams(location.search).get('publish') === 'model' ? 'publish' : null);
  const [menu, setMenu] = useState(false);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [order, setOrder] = useState("default");
  const [step, setStep] = useState(0);
  const [detailTab, setDetailTab] = useState("overview");
  const [copy, setCopy] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [reference, setReference] = useState(null);
  const close = React.useCallback(() => setModal(null), []);
  const refresh = () =>
    fetch("/api/site")
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json();
      })
      .then((data) => {
        setSite(data);
        setConnectionError(false);
      })
      .catch(() => setConnectionError(true));
  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {
    if (modal === "lgbm" && !reference)
      fetch("/api/reference")
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then(setReference)
        .catch(() => setReference({ unavailable: true }));
  }, [modal]);
  const open = (id) => {
    if (id === "publish") { window.location.assign('/community/new'); return; }
    setDetailTab("overview");
    setCopy(false);
    setPublishError("");
    setModal(id);
  };
  const scroll = (id) => {
    setMenu(false);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  };
  const allItems = [
    ...site.models.map((m) => ({
      ...m,
      id: `shared-${m.id}`,
      title: m.repo.split("/").pop(),
      owner: m.owner,
      label:
        m.status === "gone"
          ? "源文件已不可用"
          : m.deprecated
            ? "已弃用"
            : "社区模型",
      model: m.fields.base_model || "未声明",
      task: m.fields.task || "未声明任务",
      color: "mint",
      description: m.author_text || "查看模型卡、训练配置与版本信息。",
      metric:
        m.research?.rank_ic != null
          ? `${m.research.rank_ic >= 0 ? "+" : ""}${m.research.rank_ic.toFixed(4)}`
          : m.research?.net_excess_ann != null
            ? `${m.research.net_excess_ann >= 0 ? "+" : ""}${m.research.net_excess_ann.toFixed(2)}%`
            : null,
      metricLabel:
        m.research?.rank_ic != null
          ? "Rank IC · 作者自报"
          : "扣成本净超额 · 作者自报",
      secondary:
        m.research?.rank_ic != null && m.research?.net_excess_ann != null
          ? `${m.research.net_excess_ann >= 0 ? "+" : ""}${m.research.net_excess_ann.toFixed(2)}%`
          : m.research?.test_period || "未声明",
      secondaryLabel:
        m.research?.rank_ic != null && m.research?.net_excess_ann != null
          ? "扣成本净超额 · 作者自报"
          : "样本外测试区间",
      tags: [m.fields.market || "未声明市场", m.sha.slice(0, 7)],
      icon: GitBranch,
    })),
    ...templates,
  ];
  let items = allItems.filter(
    (m) =>
      (filter === "all" ||
        (filter === "community" && m.repo) ||
        (filter === "recipes" && !m.repo && m.id !== "lgbm") ||
        m.model === filter) &&
      `${m.title} ${m.owner} ${m.task} ${m.model}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  if (order === "name")
    items = [...items].sort((a, b) => a.title.localeCompare(b.title));
  const copyText = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopy(true);
      setTimeout(() => setCopy(false), 2000);
    } catch {
      setCopy(false);
    }
  };
  const recipe =
    "name: cn_rank_5d_lgbm\ntask: cn_rank_5d.yaml\ndata:\n  source: snapshot\nmodel:\n  name: lightgbm\ntrain:\n  universe: all_ex_st\noutput: runs/cn_rank_5d_lgbm";
  async function publish(e) {
    e.preventDefault();
    setPublishing(true);
    setPublishError("");
    try {
      const r = await fetch("/share", {
        method: "POST",
        body: new URLSearchParams(new FormData(e.currentTarget)),
      });
      if (!r.ok) {
        const data = await r.json();
        throw Error(data.detail || "分享失败，请稍后重试");
      }
      window.location.assign(r.url);
    } catch (error) {
      setPublishError(error.message);
      setPublishing(false);
    }
  }

  return (
    <>
      <header className="site-header">
        <div className="nav-inner">
          <Logo />
          <nav
            aria-label="主导航"
            className={menu ? "nav-links nav-open" : "nav-links"}
          >
            <button onClick={() => scroll("platform")}>平台能力</button>
            <a href="/community">研究社区</a>
            <button onClick={() => scroll("community")}>模型广场</button>
            <button
              onClick={() => {
                setFilter("recipes");
                scroll("community");
              }}
            >
              训练配方
            </button>
            <button onClick={() => open("docs")}>
              文档 <ArrowUpRight size={13} />
            </button>
          </nav>
          <div className="nav-actions">
            {site.user ? (
              <a className="login-link" href="/logout">
                {site.user} · 退出
              </a>
            ) : (
              <button className="login-link" onClick={() => open("login")}>
                登录
              </button>
            )}
            <button
              className="button button-dark nav-cta"
              onClick={() => open("start")}
            >
              开始构建 <ArrowUpRight size={15} />
            </button>
            <button
              className="icon-button mobile-menu"
              aria-label={menu ? "关闭导航" : "打开导航"}
              onClick={() => setMenu(!menu)}
            >
              {menu ? <X /> : <Menu />}
            </button>
          </div>
        </div>
      </header>
      <main>
        <Hero open={open} scroll={scroll}/>
        <section className="ecosystem" id="ecosystem">
          <div className="container ecosystem-inner">
            <p>与你熟悉的研究生态协同</p>
            <div className="ecosystem-logos">
              <span>
                <img
                  src="/brands/python.svg"
                  alt=""
                  className="ecosystem-icon"
                />{" "}
                Python
              </span>
              <span>
                <img
                  src="/brands/pytorch.svg"
                  alt=""
                  className="ecosystem-icon"
                />{" "}
                PyTorch
              </span>
              <span>
                <GitBranch size={23} /> LightGBM
              </span>
              <span>
                <img
                  src="/brands/huggingface.svg"
                  alt=""
                  className="ecosystem-icon"
                />{" "}
                Hugging Face
              </span>
              <span className="kronos-word">
                <Activity size={24} /> Kronos
              </span>
            </div>
          </div>
        </section>

        <section className="community section-space" id="community">
          <div className="container">
            <div className="section-heading">
              <div>
                <div className="eyebrow">
                  <span /> THE OPEN RESEARCH SPACE
                </div>
                <h2>好模型，值得被看见。</h2>
                <p>发现模型背后的方法，也让你的研究成为下一次探索的起点。</p>
              </div>
              <button
                className="button button-white"
                onClick={() => window.location.assign('/community')}
              >
                进入研究社区 <ArrowUpRight size={14} />
              </button>
            </div>
            <div className="community-toolbar">
              <div className="filters" role="tablist" aria-label="模型分类">
                {[
                  ["all", "全部探索"],
                  ["LightGBM", "LightGBM"],
                  ["Kronos", "Kronos"],
                  ["recipes", "训练配方"],
                  ["community", "社区分享"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    role="tab"
                    aria-selected={filter === value}
                    className={filter === value ? "active" : ""}
                    onClick={() => setFilter(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="search-sort">
                <label className="search-box">
                  <Search size={15} />
                  <input
                    placeholder="搜索模型、方法…"
                    aria-label="搜索模型"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                  {search && (
                    <button aria-label="清空搜索" onClick={() => setSearch("")}>
                      <X size={13} />
                    </button>
                  )}
                </label>
                <label className="sort-control" title="排列方式">
                  <SlidersHorizontal size={15} />
                  <select
                    value={order}
                    onChange={(e) => setOrder(e.target.value)}
                    aria-label="排列方式"
                  >
                    <option value="default">最近发布</option>
                    <option value="name">名称排序</option>
                  </select>
                </label>
              </div>
            </div>
            {connectionError && (
              <div className="connection-warning">
                社区服务暂时未连接，参考实验与训练模板仍可浏览。
                <button onClick={refresh}>重新连接</button>
              </div>
            )}
            <div className="model-grid">
              {items.map((item) => {
                const Icon = item.icon;
                return (
                  <motion.article className={`model-card ${item.color}`} key={item.id}
                    initial={reducedMotion ? false : {opacity:0,y:22}}
                    whileInView={{opacity:1,y:0}}
                    viewport={{once:true,amount:.12}}
                    transition={{duration:.6,ease:[.22,1,.36,1]}}>
                    <button
                      className="model-open"
                      aria-label={`查看${item.title}`}
                      onClick={() =>
                        item.repo
                          ? window.location.assign(
                              `/m/${item.id.replace("shared-", "")}`,
                            )
                          : open(item.id)
                      }
                    >
                      <div className="model-visual">
                        <ResearchVisual kind={item.id} label={item.label}/>
                        <span className="model-type">
                          <Icon size={12} />
                          {item.label}
                        </span>
                        <ArrowUpRight className="card-arrow" size={20} />
                      </div>
                      <div className="model-info">
                        <div className="model-owner">
                          <span className={`owner-avatar ${item.color}`}>
                            {item.repo ? (
                              item.owner.slice(0, 1).toUpperCase()
                            ) : (
                              <Icon size={10} />
                            )}
                          </span>
                          {item.owner}
                          <span className="owner-separator">/</span>
                          {item.task}
                        </div>
                        <h3>{item.title}</h3>
                        <p>{item.description}</p>
                        <div className="model-metrics">
                          {item.metric ? (
                            <>
                              <div>
                                <strong>{item.metric}</strong>
                                <span>{item.metricLabel}</span>
                              </div>
                              <div>
                                <strong>{item.secondary}</strong>
                                <span>{item.secondaryLabel}</span>
                              </div>
                            </>
                          ) : (
                            <div className="template-note">
                              <FileCode2 size={17} />
                              <span>
                                {item.id === "kronos"
                                  ? "从预训练模型，开始你的实验。"
                                  : item.repo
                                    ? "模型信息与作者方法"
                                    : "下一份研究，来自你。"}
                              </span>
                            </div>
                          )}
                        </div>
                        <div className="model-tags">
                          {item.tags.map((tag) => (
                            <span key={tag}>{tag}</span>
                          ))}
                          <ChevronRight size={15} />
                        </div>
                      </div>
                    </button>
                  </motion.article>
                );
              })}
            </div>
            {!items.length && (
              <div className="empty-state">
                <Search size={26} />
                <h3>
                  {filter === "community" && !search
                    ? "期待你的第一份研究"
                    : "没有找到匹配的模型"}
                </h3>
                <p>
                  {filter === "community"
                    ? "分享训练方法与评估报告，与其他研究者交流。"
                    : "试试其他关键词，或浏览全部模型。"}
                </p>
                <button
                  className="button button-dark"
                  onClick={() => {
                    if (filter === "community")
                      open(site.user ? "publish" : "login");
                    else {
                      setSearch("");
                      setFilter("all");
                    }
                  }}
                >
                  {filter === "community" ? "分享模型" : "查看全部"}
                  <ArrowRight size={15} />
                </button>
              </div>
            )}
            <div className="community-footnote">
              <span>
                <CircleDot size={13} />{" "}
                参考实验来自本地报告；训练模板不代表已取得业绩。
              </span>
              <button onClick={() => open("standards")}>
                了解展示标准 <ArrowRight size={13} />
              </button>
            </div>
          </div>
        </section>

        <section className="platform section-space" id="platform">
          <div className="container">
            <div className="section-heading">
              <div>
                <div className="eyebrow">
                  <span /> FROM IDEA TO EVIDENCE
                </div>
                <h2>
                  完整的研究链路。
                  <br />
                  始终由你掌控。
                </h2>
              </div>
              <p className="heading-aside">
                把时间交给研究。
                <br />
                让工作台处理数据、训练、验证与交付。
              </p>
            </div>
            <div className="workflow-tabs" role="tablist" aria-label="研究流程">
              {[
                [Database, "01", "准备数据"],
                [Cpu, "02", "训练模型"],
                [ShieldCheck, "03", "检验结果"],
                [Upload, "04", "分享研究"],
              ].map(([Icon, num, title], i) => (
                <button
                  key={title}
                  role="tab"
                  aria-selected={step === i}
                  className={step === i ? "selected" : ""}
                  onClick={() => setStep(i)}
                >
                  <span className="workflow-number">{num}</span>
                  <span>{title}</span>
                  <ChevronRight size={15} />
                </button>
              ))}
            </div>
            <div className="workflow-body">
              <div className="workflow-copy">
                <span className="small-overline">
                  {
                    [
                      "POINT-IN-TIME DATA",
                      "LOCAL-FIRST TRAINING",
                      "EVIDENCE BEFORE CONFIDENCE",
                      "RESEARCH WITHOUT BOUNDARIES",
                    ][step]
                  }
                </span>
                <h3>
                  {
                    [
                      "数据对齐时点，研究才有起点。",
                      "从第一份配置，到自己的模型。",
                      "看清表现，也看清边界。",
                      "分享结果，更分享如何得到结果。",
                    ][step]
                  }
                </h3>
                <p>
                  {
                    [
                      "接入日线快照或导入自己的数据。明确股票池、标签窗口与数据时点，为每一次训练留下可追溯的数据基础。",
                      "用清晰的任务配置驱动端到端训练。LightGBM 基准与 Kronos 微调适配器，让不同的研究路径有共同的起点。",
                      "把样本外评估、交易成本、前视检查和多种子稳定性放进同一份报告。模型的局限，同样值得被认真呈现。",
                      "把训练配方、模型卡和版本信息一起交付。通过 Hugging Face 分享，让研究方法可以被理解、讨论与复现。",
                    ][step]
                  }
                </p>
                <ul>
                  {[
                    ["时点数据契约", "本地快照与数据许可", "标签与股票池检查"],
                    ["本机 CPU / GPU", "YAML 训练配方", "多种子与滚动训练"],
                    ["扣成本净超额", "强基准同场比较", "本地评估台账"],
                    ["固定提交版本", "模型卡与训练配方", "作者说明独立呈现"],
                  ][step].map((t) => (
                    <li key={t}>
                      <CheckCircle2 size={15} />
                      {t}
                    </li>
                  ))}
                </ul>
                <button className="text-link" onClick={() => open("docs")}>
                  探索工作台 <ArrowUpRight size={15} />
                </button>
              </div>
              <div className="research-terminal">
                <div className="terminal-title">
                  <span className="terminal-dots">
                    <i />
                    <i />
                    <i />
                  </span>
                  <span>wisehold / research workspace</span>
                  <Terminal size={14} />
                </div>
                <div className="terminal-body">
                  <div className="terminal-command">
                    <span>~</span>{" "}
                    {
                      [
                        "wisehold run examples/run_cn_rank_5d.yaml --check",
                        "wisehold run examples/run_cn_rank_5d.yaml",
                        "runs/cn_rank_5d_lgbm/report.md",
                        "wisehold build package publish",
                      ][step]
                    }
                  </div>
                  <div className="terminal-comment">
                    #{" "}
                    {
                      [
                        "明确数据来源与时点",
                        "一份配置，驱动完整实验",
                        "来自项目参考实验 · 2024–2025",
                        "标准化研究交付",
                      ][step]
                    }
                  </div>
                  {step === 0 ? (
                    <>
                      <CodeRow label="market" value="CN A-share" />
                      <CodeRow label="snapshot" value="snap-49d3a62851188ff8" />
                      <CodeRow label="universe" value="all_ex_st" />
                      <CodeRow
                        label="panel"
                        value="1,942 days × 5,696 stocks"
                      />
                      <div className="terminal-result">
                        <Database size={14} /> 数据留在你的机器上
                      </div>
                    </>
                  ) : step === 1 ? (
                    <>
                      <CodeRow label="model" value="lightgbm" />
                      <CodeRow label="task" value="cn_rank_5d" />
                      <CodeRow label="seeds" value="[0, 1, 2]" />
                      <CodeRow label="validation" value="walk-forward" />
                      <div className="terminal-progress">
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                        <span />
                      </div>
                      <div className="terminal-result">
                        <CheckCircle2 size={14} /> 参考实验 · 训练与预测 257.7s
                      </div>
                    </>
                  ) : step === 2 ? (
                    <>
                      <CodeRow label="rank_ic" value="+0.1104" />
                      <CodeRow label="net_excess_ann" value="+2.7%" />
                      <CodeRow label="net_excess_t" value="0.30" />
                      <CodeRow label="seed_rank_corr" value="0.984" />
                      <div className="terminal-warning">
                        净超额未达到统计显著，仍需独立验证。
                      </div>
                    </>
                  ) : (
                    <>
                      <CodeRow
                        label="artifacts"
                        value="model + recipe + report"
                      />
                      <CodeRow label="model_card" value="README.md" />
                      <CodeRow label="revision" value="pinned commit" />
                      <CodeRow label="destination" value="Hugging Face" />
                      <div className="terminal-result">
                        <GitBranch size={14} /> 分享可追溯的研究方法
                      </div>
                    </>
                  )}
                </div>
                <div className="terminal-footer">
                  <span>
                    <span className="live-dot" /> LOCAL ENVIRONMENT
                  </span>
                  <LockKeyhole size={13} /> YOUR DATA STAYS YOURS
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="principles">
          <div className="container principles-inner">
            <div>
              <span className="principle-index" aria-hidden="true">01</span>
              <h3>本地优先</h3>
              <p>
                数据与算力留在本机。
                <br />
                研究资产，由你掌握。
              </p>
            </div>
            <div>
              <span className="principle-index" aria-hidden="true">02</span>
              <h3>证据驱动</h3>
              <p>
                结果与检验一同呈现。
                <br />
                不把预测能力等同于收益。
              </p>
            </div>
            <div>
              <span className="principle-index" aria-hidden="true">03</span>
              <h3>方法开放</h3>
              <p>
                配置、环境与版本可追溯。
                <br />
                让研究建立在彼此的工作之上。
              </p>
            </div>
          </div>
        </section>
        <section className="closing">
          <div className="container closing-inner">
            <div className="eyebrow">YOUR NEXT DISCOVERY STARTS HERE</div>
            <h2>
              训练你的判断。
              <br />
              分享你的证据。
            </h2>
            <p>下一份值得复现的研究，从你的工作台开始。</p>
            <button
              className="button button-dark button-large"
              onClick={() => open("start")}
            >
              开始我的研究 <ArrowUpRight size={17} />
            </button>
            <div className="closing-word" aria-hidden="true">
              WiseHold.
            </div>
          </div>
        </section>
      </main>
      <footer>
        <div className="container">
          <div className="footer-top">
            <Logo />
            <span>独立研究，共同进步。</span>
            <div>
              <button onClick={() => open("docs")}>使用文档</button>
              <button onClick={() => open("standards")}>社区标准</button>
              <button onClick={() => open(site.user ? "publish" : "login")}>
                分享研究 <ArrowUpRight size={13} />
              </button>
            </div>
          </div>
          <div className="footer-bottom">
            <span>© {new Date().getFullYear()} WiseHold</span>
            <p>
              量化研究与技术交流平台。历史回测不代表未来表现，不构成投资建议。
            </p>
            <span className="footer-status">
              <span className="live-dot" />{" "}
              {connectionError ? "离线浏览" : "本地优先 · 开放研究"}
            </span>
          </div>
        </div>
      </footer>

      {modal && (
        <Modal
          title={
            modal === "start"
              ? "开始你的下一次研究"
              : modal === "login"
                ? "让你的研究被看见"
                : modal === "publish"
                  ? "分享你的模型"
                  : modal === "docs"
                    ? "从这里，进入工作台"
                    : modal === "standards"
                      ? "每一份研究，都有清晰的边界"
                      : templates.find((t) => t.id === modal)?.title ||
                        "研究详情"
          }
          subtitle={
            modal === "start"
              ? "本地训练，完整控制。选择适合你的研究起点。"
              : modal === "login"
                ? "使用 Hugging Face 身份连接你的模型与研究方法。"
                : null
          }
          close={close}
          wide={["lgbm", "docs"].includes(modal)}
        >
          {modal === "start" && (
            <>
              <div className="start-options">
                <a
                  className="start-option"
                  href="http://127.0.0.1:8765"
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="option-icon">
                    <Terminal size={22} />
                  </span>
                  <div>
                    <h3>打开本地工作台</h3>
                    <p>已安装并启动 WiseHold · localhost:8765</p>
                  </div>
                  <ArrowUpRight size={18} />
                </a>
                <button className="start-option" onClick={() => open("docs")}>
                  <span className="option-icon mint">
                    <BookOpen size={22} />
                  </span>
                  <div>
                    <h3>第一次使用？</h3>
                    <p>安装说明、数据准备与第一份训练配方</p>
                  </div>
                  <ArrowRight size={18} />
                </button>
              </div>
              <p className="form-note">
                本地工作台需先运行项目目录中的 start.bat（Windows）或
                start.sh（Linux）。
              </p>
              <button
                className="text-link"
                onClick={() => {
                  close();
                  scroll("community");
                }}
              >
                先探索模型与方法 <ArrowRight size={15} />
              </button>
            </>
          )}
          {modal === "login" && (
            <>
              <div className="login-symbol">
                <Users size={35} />
                <span>↗</span>
              </div>
              <p className="modal-text">
                分享模型卡、固定版本和训练方法。模型权重保存在你的 Hugging Face
                仓库，平台展示公开的研究信息。
              </p>
              {connectionError ? (
                <p className="form-error">社区服务暂时不可用，请稍后重试。</p>
              ) : (
                <a className="button button-dark full-width" href="/login">
                  {site.dev ? "进入本地开发账号" : "使用 Hugging Face 登录"}{" "}
                  <ArrowUpRight size={17} />
                </a>
              )}
              {site.dev && (
                <p className="form-note">
                  当前为本机开发环境，尚未配置正式 Hugging Face OAuth。
                </p>
              )}
            </>
          )}
          {modal === "publish" && (
            <form onSubmit={publish} className="publish-form">
              <input type="hidden" name="token" value={site.csrf} />
              <label>
                Hugging Face 模型仓库
                <input
                  name="repo"
                  placeholder="your-name / your-model"
                  required
                  maxLength={200}
                />
              </label>
              <label>
                训练方式与研究说明
                <textarea
                  name="author_text"
                  rows={6}
                  maxLength={5000}
                  placeholder="描述数据范围、训练配方、样本外验证方法及已知局限…"
                />
              </label>
              <details className="research-form-details">
                <summary>
                  训练配方与业绩指标（选填） <ChevronDown size={15} />
                </summary>
                <label>
                  样本外测试区间
                  <input
                    name="test_period"
                    maxLength={80}
                    placeholder="例如：2024-01 至 2025-12"
                  />
                </label>
                <div className="metric-inputs">
                  <label>
                    Rank IC
                    <input
                      name="rank_ic"
                      type="number"
                      min="-1"
                      max="1"
                      step="any"
                      placeholder="0.0000"
                    />
                  </label>
                  <label>
                    扣成本年化净超额（%）
                    <input
                      name="net_excess_ann"
                      type="number"
                      min="-100"
                      max="10000"
                      step="any"
                      placeholder="0.00"
                    />
                  </label>
                </div>
                <label>
                  训练配方
                  <textarea
                    name="recipe_text"
                    rows={5}
                    maxLength={8000}
                    placeholder="粘贴 YAML 配置或描述训练步骤…"
                  />
                </label>
                <p className="form-note">
                  指标将标为作者自报。填写指标时须注明测试区间，并在研究说明中列明基准、交易成本与已知局限。
                </p>
              </details>
              <p className="form-note">
                仓库需公开，README 须包含 YAML
                模型卡信息。分享将绑定到当前提交版本。
              </p>
              {publishError && (
                <p role="alert" className="form-error">
                  {publishError}
                </p>
              )}
              <button
                className="button button-dark full-width"
                disabled={publishing}
              >
                {publishing ? "正在读取模型卡…" : "发布研究"}
                <Upload size={16} />
              </button>
            </form>
          )}
          {modal === "docs" && (
            <div className="docs-content">
              <div className="doc-step">
                <span>01</span>
                <div>
                  <h3>准备本地环境</h3>
                  <p>
                    在 WiseHold 项目目录运行安装脚本。Windows 使用
                    install.bat；Linux 使用 sh install.sh。先安装{" "}
                    <a
                      href="https://docs.astral.sh/uv/getting-started/installation/"
                      target="_blank"
                      rel="noreferrer"
                    >
                      uv <ExternalLink size={11} />
                    </a>
                    。发布包已包含扣成本检验依赖，安装脚本会自动安装。
                  </p>
                </div>
              </div>
              <div className="doc-step">
                <span>02</span>
                <div>
                  <h3>启动工作台</h3>
                  <p>
                    运行 start.bat 或 sh
                    start.sh，打开本地工作台。先用合成数据试运行，再准备自己的数据快照。
                  </p>
                  <a
                    className="text-link"
                    href="http://127.0.0.1:8765"
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开 localhost:8765 <ArrowUpRight size={14} />
                  </a>
                </div>
              </div>
              <div className="doc-step">
                <span>03</span>
                <div>
                  <h3>训练第一份模型</h3>
                  <p>
                    通过本地界面配置任务，或使用命令行运行项目提供的训练配方。
                  </p>
                  <div className="copy-code">
                    <code>wisehold run examples/run_cn_rank_5d.yaml</code>
                    <button
                      className="icon-button"
                      title={copy ? "已复制" : "复制命令"}
                      aria-label="复制命令"
                      onClick={() =>
                        copyText("wisehold run examples/run_cn_rank_5d.yaml")
                      }
                    >
                      {copy ? <Check size={15} /> : <Copy size={15} />}
                    </button>
                  </div>
                </div>
              </div>
              <div className="doc-step">
                <span>04</span>
                <div>
                  <h3>评估并分享</h3>
                  <p>
                    先检查标准报告中的样本外指标、成本与前视检查，再把模型包发布到公开
                    Hugging Face
                    仓库，在研究社区发布讨论并关联模型，展示固定版本与训练说明。
                  </p>
                </div>
              </div>
            </div>
          )}
          {modal === "standards" && (
            <div className="standards">
              <p>模型表现需要和获得它的条件一起阅读。</p>
              {[
                [
                  "清楚区分实验与模板",
                  "参考实验引用项目报告。训练模板用于启动研究，不附加虚构业绩或用户数据。",
                ],
                [
                  "预测指标与收益分开呈现",
                  "Rank IC 衡量排序能力；回测收益须说明测试区间、基准、交易成本与成交约束。",
                ],
                [
                  "工具信息与作者说明分开",
                  "模型卡来自固定提交。作者方法与结论为自述，平台不做独立认证。",
                ],
                [
                  "保留版本与已知局限",
                  "缺少字段、已弃用版本与不可用源文件会明确标注。台账为本地自报，不能证明记录未被删除。",
                ],
              ].map(([t, p]) => (
                <div key={t}>
                  <ShieldCheck size={19} />
                  <section>
                    <h3>{t}</h3>
                    <p>{p}</p>
                  </section>
                </div>
              ))}
            </div>
          )}
          {modal === "lgbm" && (
            <>
              <div className="detail-meta">
                <span className="tag">参考实验</span>
                <span>WiseHold Research</span>
                <span>最终测试 2024–2025</span>
              </div>
              <div className="detail-tabs" role="tablist" aria-label="实验详情">
                {[
                  ["overview", "评估结果"],
                  ["recipe", "训练配方"],
                  ["report", "实验报告"],
                ].map(([id, label]) => (
                  <button
                    role="tab"
                    aria-selected={detailTab === id}
                    className={detailTab === id ? "active" : ""}
                    key={id}
                    onClick={() => setDetailTab(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {detailTab === "overview" ? (
                <>
                  <div className="detail-stats">
                    <div>
                      <span>样本外 Rank IC</span>
                      <strong>+0.1104</strong>
                      <small>Newey-West t = 8.65</small>
                    </div>
                    <div>
                      <span>扣成本年化净超额</span>
                      <strong>+2.7%</strong>
                      <small>t = 0.30 · 未达统计显著</small>
                    </div>
                    <div>
                      <span>种子间预测秩相关</span>
                      <strong>0.984</strong>
                      <small>3 个随机种子</small>
                    </div>
                  </div>
                  <h3 className="detail-section-title">同口径基准比较</h3>
                  <div className="comparison-chart">
                    {[
                      ["LightGBM 集成", 2.7],
                      ["5 日反转", -9.6],
                      ["20 日动量", -44.6],
                      ["低波动", -8.7],
                    ].map(([name, value]) => (
                      <div className="comparison-row" key={name}>
                        <span>{name}</span>
                        <div className="bar-track">
                          <span
                            className={value > 0 ? "positive" : "negative"}
                            style={{
                              width: `${(Math.abs(value) / 55) * 100}%`,
                              left: `${((50 + Math.min(value, 0)) / 55) * 100}%`,
                            }}
                          />
                        </div>
                        <strong>
                          {value > 0 ? "+" : ""}
                          {value.toFixed(1)}%
                        </strong>
                      </div>
                    ))}
                  </div>
                  <p className="chart-note">
                    扣成本年化净超额 · 相对股票池等权基准（年化 +27.4%）
                  </p>
                  <div className="evidence-note">
                    <ShieldCheck size={19} />
                    <p>
                      排序能力显著，扣成本后的净超额不显著。回测含价差、佣金与印花税，未计入大单冲击成本。本结果不代表未来可实现收益。
                    </p>
                  </div>
                  <div className="detail-facts">
                    <span>全 A 股剔除 ST</span>
                    <span>每 5 个交易日调仓</span>
                    <span>前 10% 等权持仓</span>
                    <span>按年滚动重训</span>
                  </div>
                </>
              ) : detailTab === "recipe" ? (
                <>
                  <div className="recipe-toolbar">
                    <span>
                      <FileCode2 size={14} /> run_cn_rank_5d.yaml
                    </span>
                    <button
                      className="text-link"
                      onClick={() => copyText(recipe)}
                    >
                      {copy ? <Check size={14} /> : <Copy size={14} />}{" "}
                      {copy ? "已复制" : "复制配方"}
                    </button>
                  </div>
                  <pre className="recipe-code">{recipe}</pre>
                  <a className="button button-white" href="/downloads/recipe">
                    <ArrowDownToLine size={15} /> 下载训练配方
                  </a>
                </>
              ) : reference ? (
                reference.unavailable ? (
                  <p className="form-error">实验报告暂不可用。</p>
                ) : (
                  <>
                    <a className="text-link" href="/downloads/report">
                      <ArrowDownToLine size={14} /> 下载 Markdown 报告
                    </a>
                    <pre className="report-text">{reference.report}</pre>
                  </>
                )
              ) : (
                <p>正在读取报告…</p>
              )}
            </>
          )}
          {modal === "kronos" && (
            <div className="template-detail">
              <div className="template-detail-icon lavender">
                <Activity size={35} />
              </div>
              <p>
                基于 Kronos
                时序基础模型的本地微调路径。项目提供适配器与任务配置，适合探索时序表示与量化预测。
              </p>
              <div className="evidence-note">
                <FileCode2 size={18} />
                <p>
                  这是训练模板，不附带公开的业绩声明。请在自己的数据和独立测试区间完成评估。
                </p>
              </div>
              <button
                className="button button-dark"
                onClick={() => open("docs")}
              >
                查看训练准备 <ArrowRight size={15} />
              </button>
              <a
                className="text-link"
                href="https://huggingface.co/NeoQuasar/Kronos-small"
                target="_blank"
                rel="noreferrer"
              >
                查看基础模型 <ExternalLink size={14} />
              </a>
            </div>
          )}
          {modal === "custom" && (
            <div className="template-detail">
              <div className="template-detail-icon peach">
                <Braces size={35} />
              </div>
              <p>
                让其他研究者看到你的训练方式、模型版本、测试区间与完整报告。公开模型仓库后，即可把你的研究加入社区。
              </p>
              <button
                className="button button-dark"
                onClick={() => open(site.user ? "publish" : "login")}
              >
                分享我的模型 <Upload size={16} />
              </button>
            </div>
          )}
        </Modal>
      )}
    </>
  );
}

function CodeRow({ label, value }) {
  return (
    <div className="code-row">
      <span>{label}</span>
      <span>{value}</span>
    </div>
  );
}

createRoot(document.getElementById("root")).render(location.pathname.startsWith('/community') ? <Forum/> : <App />);
