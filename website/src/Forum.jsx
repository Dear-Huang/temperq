import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  Code2,
  FileText,
  FlaskConical,
  GitBranch,
  Layers3,
  LogIn,
  MessageCircle,
  Plus,
  Search,
  Send,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import "./forum.css";
import "./forum-theme.css";

const categories = {
  models: ["模型分享", Layers3],
  recipes: ["训练方法", Code2],
  performance: ["业绩复盘", FlaskConical],
  questions: ["问题讨论", MessageCircle],
};
const date = (value) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
async function api(url, options) {
  const r = await fetch(url, options);
  const data = await r.json();
  if (!r.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : "内容格式不正确，请检查后重试",
    );
  return data;
}
function Avatar({ name }) {
  return <span className="forum-avatar">{name.slice(0, 1).toUpperCase()}</span>;
}

export default function Forum() {
  const [site, setSite] = useState(null),
    [topics, setTopics] = useState([]),
    [topic, setTopic] = useState(null);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState(
    new URLSearchParams(location.search).get("category") || "all",
  );
  const [search, setSearch] = useState(""),
    [sort, setSort] = useState("active"),
    [login, setLogin] = useState(false);
  const topicId = location.pathname.match(/^\/community\/t\/(\d+)$/)?.[1];
  const composing = location.pathname === "/community/new";
  const [draft, setDraft] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem("qwb-topic-draft")) || {};
    } catch {
      return {};
    }
  });
  const [reply, setReply] = useState("");
  useEffect(() => {
    document.title = `${composing ? "发布研究" : topicId ? "研究讨论" : "研究社区"} · Quant Workbench`;
    Promise.all([
      api("/api/site"),
      api("/api/forum"),
      topicId ? api(`/api/forum/${topicId}`) : Promise.resolve(null),
    ])
      .then(([s, f, t]) => {
        setSite(s);
        setTopics(f.topics);
        setTopic(t);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    if (composing)
      sessionStorage.setItem("qwb-topic-draft", JSON.stringify(draft));
  }, [draft, composing]);
  function auth() {
    if (site?.user) location.assign("/community/new");
    else setLogin(true);
  }
  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = new FormData(e.currentTarget);
      if (!data.get("share_id")) data.delete("share_id");
      data.set("token", site.csrf);
      const result = await api("/api/forum", { method: "POST", body: data });
      sessionStorage.removeItem("qwb-topic-draft");
      location.assign(result.url);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }
  async function sendReply(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(`/api/forum/${topicId}/replies`, {
        method: "POST",
        body: new URLSearchParams({ body: reply, token: site.csrf }),
      });
      setReply("");
      setTopic(await api(`/api/forum/${topicId}`));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  let visible = topics.filter(
    (t) =>
      (filter === "all" ||
        (filter === "mine" && t.owner === site?.user) ||
        t.category === filter) &&
      `${t.title} ${t.excerpt} ${t.owner}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  if (sort === "new") visible = [...visible].sort((a, b) => b.id - a.id);
  if (sort === "replies")
    visible = [...visible].sort((a, b) => b.reply_count - a.reply_count);
  const update = (key, value) => setDraft((d) => ({ ...d, [key]: value }));
  return (
    <div className="forum-app">
      <header className="forum-header">
        <a className="brand" href="/">
          <Layers3 size={23} />
          <span>
            quant<span className="brand-slash">/</span>workbench
          </span>
        </a>
        <nav>
          <a href="/">产品首页</a>
          <a href="/community" className="current">
            研究社区
          </a>
          <a href="/#community">
            模型广场 <ArrowUpRight size={12} />
          </a>
        </nav>
        <div>
          {site?.user ? (
            <>
              <span className="forum-account">
                <Avatar name={site.user} />
                {site.user}
              </span>
              <a href="/logout">退出</a>
            </>
          ) : (
            <button onClick={() => setLogin(true)}>
              登录 <LogIn size={14} />
            </button>
          )}
        </div>
      </header>
      <div className="forum-layout">
        <aside className="forum-sidebar">
          <div className="forum-space-name">
            <span className="forum-space-icon">
              <Users size={21} />
            </span>
            <div>
              <strong>研究社区</strong>
              <small>QUANT WORKBENCH COMMUNITY</small>
            </div>
          </div>
          <button className="button button-dark forum-publish" onClick={auth}>
            <Plus size={17} /> 发布研究
          </button>
          <div className="forum-nav-label">探索讨论</div>
          <a
            href="/community"
            className={
              !topicId && !composing && filter === "all" ? "selected" : ""
            }
            onClick={(e) => {
              if (!topicId && !composing) {
                e.preventDefault();
                setFilter("all");
              }
            }}
          >
            <MessageCircle size={17} />
            全部讨论<span>{topics.length}</span>
          </a>
          {Object.entries(categories).map(([key, [label, Icon]]) => (
            <a
              key={key}
              className={
                filter === key && !topicId && !composing ? "selected" : ""
              }
              href={`/community?category=${key}`}
              onClick={(e) => {
                if (!topicId && !composing) {
                  e.preventDefault();
                  setFilter(key);
                }
              }}
            >
              <Icon size={17} />
              {label}
              <span>{topics.filter((t) => t.category === key).length}</span>
            </a>
          ))}
          <div className="forum-nav-label">我的空间</div>
          <button
            className={filter === "mine" ? "selected" : ""}
            onClick={() => {
              if (!site?.user) setLogin(true);
              else if (topicId || composing)
                location.assign("/community?category=mine");
              else setFilter("mine");
            }}
          >
            <FileText size={17} />
            我的研究
          </button>
          <div className="sidebar-bottom">
            <ShieldCheck size={17} />
            <p>
              方法可复现，结果有边界。
              <br />
              让每一次交流都有据可循。
            </p>
            <a href="/">
              了解工作台 <ArrowUpRight size={12} />
            </a>
          </div>
        </aside>
        <main className="forum-main">
          {(topicId || composing) && (
            <a className="forum-back" href="/community">
              <ArrowLeft size={14} /> 返回研究社区
            </a>
          )}
          {loading ? (
            <div className="forum-loading">正在读取研究社区…</div>
          ) : composing ? (
            <>
              <div className="forum-page-heading">
                <span>SHARE YOUR RESEARCH</span>
                <h1>发布一份研究</h1>
                <p>把模型、训练方法与研究发现，带到讨论中来。</p>
              </div>
              {!site?.user ? (
                <div className="forum-login-state">
                  <Users size={30} />
                  <h2>连接你的研究身份</h2>
                  <p>登录后即可发布，当前草稿会保存在本次浏览器会话中。</p>
                  <button
                    className="button button-dark"
                    onClick={() => setLogin(true)}
                  >
                    登录并继续 <ArrowRight size={15} />
                  </button>
                </div>
              ) : (
                <form className="topic-composer" onSubmit={submit}>
                  <div className="composer-row">
                    <label>
                      讨论板块
                      <select
                        name="category"
                        value={draft.category || "models"}
                        onChange={(e) => update("category", e.target.value)}
                      >
                        {Object.entries(categories).map(([k, [l]]) => (
                          <option key={k} value={k}>
                            {l}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      关联已发布模型（选填）
                      <select
                        name="share_id"
                        value={draft.share_id || ""}
                        onChange={(e) => update("share_id", e.target.value)}
                      >
                        <option value="">不关联模型</option>
                        {site.models
                          .filter((m) => m.owner === site.user)
                          .map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.repo} @{m.sha.slice(0, 7)}
                            </option>
                          ))}
                      </select>
                    </label>
                  </div>
                  <p className="model-upload-hint">
                    <GitBranch size={14} /> 还没有模型记录？
                    <a href="/?publish=model">
                      导入 Hugging Face 模型 <ArrowUpRight size={12} />
                    </a>
                  </p>
                  <label>
                    标题
                    <input
                      name="title"
                      minLength={4}
                      maxLength={120}
                      required
                      placeholder="一个具体的问题，或一份值得交流的研究"
                      value={draft.title || ""}
                      onChange={(e) => update("title", e.target.value)}
                    />
                  </label>
                  <label>
                    研究正文
                    <textarea
                      name="body"
                      minLength={10}
                      maxLength={30000}
                      required
                      rows={14}
                      placeholder="分享你的研究背景、数据范围、训练方式、评估结果与已知局限…"
                      value={draft.body || ""}
                      onChange={(e) => update("body", e.target.value)}
                    />
                  </label>
                  <label>
                    训练配方（选填）
                    <textarea
                      name="recipe"
                      className="composer-code"
                      maxLength={8000}
                      rows={5}
                      placeholder="YAML 配置 / 超参数 / 复现步骤"
                      value={draft.recipe || ""}
                      onChange={(e) => update("recipe", e.target.value)}
                    />
                  </label>
                  <div className="composer-footer">
                    <span>
                      <Check size={13} /> 草稿保存在当前会话
                    </span>
                    <button className="button button-dark" disabled={busy}>
                      {busy ? "正在发布…" : "发布研究"}
                      <Send size={15} />
                    </button>
                  </div>
                </form>
              )}
            </>
          ) : topicId && topic ? (
            <article className="topic-detail">
              <div className="topic-category">
                {categories[topic.category]?.[0]}
              </div>
              <h1>{topic.title}</h1>
              <div className="topic-author">
                <Avatar name={topic.owner} />
                <strong>{topic.owner}</strong>
                <span>发布于 {date(topic.created_at)}</span>
              </div>
              <div className="topic-body">{topic.body}</div>
              {topic.model && (
                <a className="linked-model" href={`/m/${topic.model.id}`}>
                  <GitBranch size={21} />
                  <div>
                    <small>关联模型 · 固定版本</small>
                    <strong>{topic.model.repo}</strong>
                    <span>@{topic.model.sha.slice(0, 7)}</span>
                  </div>
                  <ArrowUpRight size={17} />
                </a>
              )}
              {topic.recipe && (
                <section className="topic-recipe">
                  <h2>
                    <Code2 size={17} /> 训练配方
                  </h2>
                  <pre>{topic.recipe}</pre>
                </section>
              )}
              <div className="discussion-heading">
                <h2>
                  讨论 <span>{topic.replies.length}</span>
                </h2>
                <span>交流方法，共同验证。</span>
              </div>
              {topic.replies.length ? (
                topic.replies.map((r, i) => (
                  <div className="forum-reply" key={r.id}>
                    <Avatar name={r.owner} />
                    <div>
                      <div className="reply-meta">
                        <strong>{r.owner}</strong>
                        {r.owner === topic.owner && (
                          <span className="author-badge">作者</span>
                        )}
                        <time>{date(r.created_at)}</time>
                        <small>#{i + 1}</small>
                      </div>
                      <p>{r.body}</p>
                    </div>
                  </div>
                ))
              ) : (
                <div className="no-replies">
                  <MessageCircle size={20} />
                  <p>还没有回复，留下你的第一个想法。</p>
                </div>
              )}
              {site?.user ? (
                <form className="reply-composer" onSubmit={sendReply}>
                  <label htmlFor="reply-body">参与讨论</label>
                  <textarea
                    id="reply-body"
                    rows={5}
                    required
                    maxLength={5000}
                    placeholder="你的问题、复现结果或改进建议…"
                    value={reply}
                    onChange={(e) => setReply(e.target.value)}
                  />
                  <button className="button button-dark" disabled={busy}>
                    {busy ? "正在发送…" : "发布回复"}
                    <Send size={14} />
                  </button>
                </form>
              ) : (
                <div className="reply-login">
                  <p>登录，与研究者继续讨论。</p>
                  <button
                    className="button button-dark"
                    onClick={() => setLogin(true)}
                  >
                    登录参与 <ArrowRight size={14} />
                  </button>
                </div>
              )}
            </article>
          ) : !error ? (
            <>
              <div className="forum-page-heading">
                <span>THE OPEN RESEARCH SPACE</span>
                <h1>
                  {filter === "mine"
                    ? "我的研究"
                    : categories[filter]?.[0] || "让研究，在交流中向前。"}
                </h1>
                <p>分享模型与训练方式，讨论表现与边界，寻找下一次突破。</p>
              </div>
              <a className="forum-reference" href="/#community">
                <div className="reference-symbol">
                  <FlaskConical size={25} />
                </div>
                <div>
                  <span>官方参考实验</span>
                  <h2>LightGBM 截面排序：从训练到扣成本评估</h2>
                  <p>Rank IC +0.1104 · 扣成本净超额 +2.7%（未达统计显著）</p>
                </div>
                <ArrowUpRight size={18} />
              </a>
              <div className="forum-list-toolbar">
                <div className="forum-order">
                  {[
                    ["active", "最新活跃"],
                    ["new", "最新发布"],
                    ["replies", "最多回复"],
                  ].map(([v, l]) => (
                    <button
                      key={v}
                      className={sort === v ? "active" : ""}
                      onClick={() => setSort(v)}
                    >
                      {l}
                    </button>
                  ))}
                </div>
                <label>
                  <Search size={15} />
                  <input
                    aria-label="搜索社区讨论"
                    placeholder="搜索讨论、作者…"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </label>
              </div>
              <div className="forum-topic-list">
                {visible.length ? (
                  visible.map((t) => (
                    <a
                      className="topic-row"
                      href={`/community/t/${t.id}`}
                      key={t.id}
                    >
                      <Avatar name={t.owner} />
                      <div className="topic-row-main">
                        <div className="topic-row-title">
                          <span>{categories[t.category]?.[0]}</span>
                          <h2>{t.title}</h2>
                        </div>
                        <p>{t.excerpt}</p>
                        <div className="topic-row-meta">
                          <strong>{t.owner}</strong>
                          <span>{date(t.updated_at)}</span>
                          {t.repo && (
                            <span>
                              <GitBranch size={11} />
                              {t.repo}
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="topic-reply-count">
                        <MessageCircle size={16} />
                        <strong>{t.reply_count}</strong>
                        <span>回复</span>
                      </div>
                    </a>
                  ))
                ) : (
                  <div className="forum-empty">
                    <MessageCircle size={32} />
                    <h2>
                      {search ? "没有找到匹配的讨论" : "第一份分享，从你开始。"}
                    </h2>
                    <p>
                      {search
                        ? "换个关键词试试。"
                        : "模型实验、训练心得、业绩复盘或一个好问题，都值得交流。"}
                    </p>
                    <button
                      className="button button-dark"
                      onClick={search ? () => setSearch("") : auth}
                    >
                      {search ? "清空搜索" : "发布第一篇研究"}
                      <Plus size={15} />
                    </button>
                  </div>
                )}
              </div>
            </>
          ) : null}
          {error && (
            <div className="forum-error" role="alert">
              {error}
              <button onClick={() => setError("")} aria-label="关闭提示">
                <X size={15} />
              </button>
            </div>
          )}
        </main>
        <aside className="forum-right">
          <section>
            <span className="forum-nav-label">研究者的共同空间</span>
            <h2>
              分享方法，
              <br />
              也分享思考的过程。
            </h2>
            <p>
              从第一次训练到一次失败的复现，每个真实的研究过程都值得被讨论。
            </p>
            <button className="forum-text-link" onClick={auth}>
              开始一篇分享 <ArrowRight size={14} />
            </button>
          </section>
          <section className="forum-boards">
            <h3>板块</h3>
            {Object.entries(categories).map(([k, [l, Icon]]) => (
              <a href={`/community?category=${k}`} key={k}>
                <Icon size={16} />
                <div>
                  <strong>{l}</strong>
                  <p>
                    {
                      {
                        models: "模型、版本与实验结果",
                        recipes: "配置、超参数与复现步骤",
                        performance: "测试口径、成本与局限",
                        questions: "研究问题与技术交流",
                      }[k]
                    }
                  </p>
                </div>
                <ChevronRight size={13} />
              </a>
            ))}
          </section>
          <section className="forum-etiquette">
            <ShieldCheck size={18} />
            <h3>有证据的交流</h3>
            <p>
              业绩请注明测试区间、成本和基准。区分作者自报与独立验证，尊重每一份研究。
            </p>
          </section>
          <a href="/" className="forum-text-link">
            返回产品首页 <ArrowUpRight size={13} />
          </a>
        </aside>
      </div>
      {login && (
        <div
          className="modal-backdrop"
          onClick={(e) => e.target === e.currentTarget && setLogin(false)}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="社区登录"
          >
            <button
              className="icon-button modal-close"
              aria-label="关闭登录"
              onClick={() => setLogin(false)}
            >
              <X size={18} />
            </button>
            <div className="eyebrow">JOIN THE CONVERSATION</div>
            <h2>与研究者，继续讨论。</h2>
            <p className="modal-text">
              使用 Hugging Face 身份发布研究、分享训练方法和参与回复。
            </p>
            <a
              className="button button-dark full-width"
              href={`/login?next=${encodeURIComponent(composing ? "/community/new" : location.pathname)}`}
            >
              {site?.dev ? "进入本地开发账号" : "使用 Hugging Face 登录"}
              <ArrowUpRight size={16} />
            </a>
            {site?.dev && <p className="form-note">当前为本地开发环境。</p>}
          </section>
        </div>
      )}
    </div>
  );
}
