"""Read-only onboarding and shared explanations; no trading or training policy."""
from __future__ import annotations

import html
from urllib.parse import urlencode

from . import experiments as ex, strategy, workflow_ui, external_signals


def esc(value):
    return html.escape(str(value), quote=True)


def modified(path):
    # Workers may publish/replace artifacts while the user opens the workbench.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


STEPS = (
    ("模型", "训练或选择，给股票打分"),
    ("政策", "按持仓和纪律决定怎么买"),
    ("检验", "看历史表现与成本差异"),
    ("部署", "开启纸面运行，在看板持续跟踪"),
)


def flow(step=0, base=""):
    links = ["/strategies/new", f"{base}/edit", f"{base}#history", f"{base}/dashboard"]
    parts = []
    for index, (name, meaning) in enumerate(STEPS, 1):
        content = f'<b>{index:02d} · {name}</b><small>{meaning}</small>'
        attrs = ' aria-current="step"' if index == step else ''
        if base and (index < step or index == step == 4):
            content = f'<a href="{links[index-1]}">{content}</a>'
        parts.append(f'<li{attrs}>{content}</li>')
    return '<ol class="experience-flow" aria-label="从模型到运行">'+''.join(parts)+'</ol>'


def signal_options(workdir):
    """Metadata discovery only; freezing later verifies files and implementation."""
    # Prefer the named latest copy to its archive; both share a run identity.
    paths = [*sorted((workdir / "runs").glob("*/predictions.parquet"), key=modified, reverse=True),
             *sorted((workdir / "runs/history").glob("*/predictions.parquet"), key=modified, reverse=True)]
    found, seen = {}, set()
    for path in paths:
        source = path.parent
        try:
            meta = ex.read(source / "final_model/meta.json")
            record = ex.read(source / "run.json") if (source / "run.json").exists() else {}
            identity = meta.get("run_id") or source.name
            if identity in seen:
                continue
            seen.add(identity)
            alias = source.with_name(record.get("name", source.name))
            if workflow_ui.hidden(workdir, source) or workflow_ui.hidden(workdir, alias):
                continue
            if meta.get('model')=='external_scores':external_signals.validate_meta(meta)
            else:strategy.locked_recipe(meta)
            found[source.name] = {"name": source.name, "model": meta["model"],
                                  "data_end": meta["data_end"]}
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return found


def register(app, workdir, *, page, datasets):
    @app.get("/start")
    def start():
        available = {k: v for k, v in datasets(workdir).items() if k != "_trial"}
        models = signal_options(workdir)
        projects, unreadable = [], 0
        for identity in (workdir / "runs/strategies").glob("*/identity.json"):
            try:
                versions = sorted(identity.parent.glob("versions/*/spec.json"),
                                  key=modified, reverse=True)
                if not versions:
                    continue
                for index, version in enumerate(versions):
                    root = version.parent
                    running = (root / "runtime.json").exists()
                    if index and not running:
                        continue
                    spec = ex.read(version)
                    runtime = ex.read(root / "runtime.json") if running else {}
                    health = ex.read(root / "health.json") if (root / "health.json").exists() else {}
                    demo = runtime.get("scope") == "historical_replay"
                    projects.append({"name": spec["name"], "url": f'/strategies/{spec["sid"]}/{spec["vid"]}'+('/dashboard' if running else ''),
                        "priority": 2 if running and not demo else 1 if running else 0,
                        "label": "历史演示" if demo else "已启用运行" if running else "配置与检验",
                        "message": health.get("message", "设置已保存，下一步做历史检验"),
                        "updated": modified(version)})
            except (ValueError, OSError, KeyError, TypeError):
                unreadable += 1
        projects.sort(key=lambda row: (row["priority"], row["updated"]), reverse=True)
        body = '<div class="page-heading"><div><p class="lede">本地可视化训练 · 由你定义策略</p><h1>你的量化工作台</h1><p class="mut">先用默认值走完，再把自己的方法加进去。</p></div><a href="/strategies">查看全部策略 →</a></div>'
        body += flow()
        if projects:
            body += '<section class="experience-panel"><h2>继续上次的工作</h2><div class="experience-projects">'
            for project in projects[:4]:
                body += f'<a class="experience-project" href="{esc(project["url"])}"><span class="tag">{project["label"]}</span><strong>{esc(project["name"])}</strong><small>{esc(project["message"])}</small><span>打开策略 →</span></a>'
            body += '</div></section>'
        if unreadable:
            body += '<p class="warn">部分策略元数据无法读取，请在“我的策略”中检查；其他项目仍可使用。</p>'
        train_url = '/?' + urlencode({"ds": next(iter(available))}) if available else '/#free-data'
        body += '<section class="experience-paths" aria-label="开始方式"><article class="box"><span class="experience-kicker">从平台范式开始</span><h2>训练我的第一个模型</h2><p>选数据和模型，其他先用默认设置。训练完成后继续配置政策，不需要写代码。</p>'
        body += f'<p class="mut">{"已发现本地数据，下一步选择并训练。" if available else "先用 BaoStock 下载免费日线；也可以导入自己的数据。"}</p><a class="btn primary" href="{esc(train_url)}">{"选择数据，开始训练" if available else "准备数据并训练"}</a> <a class="experience-secondary" href="/?ds=_trial#step1">先试运行演示</a><p class="mut">演示用合成数据，不能作为真实前向记录。</p></article>'
        body += '<article class="box"><span class="experience-kicker">已有模型，继续构建</span><h2>用我的模型配置策略</h2><p>沿用训练结果，从账户资金、已有持仓和交易纪律开始。参数可展开修改，冻结配置可导出。</p>'
        if models and available:
            body += f'<p class="mut">发现 {len(models)} 个符合当前五日治理口径的模型、{len(available)} 份数据。</p><a class="btn secondary" href="/strategies/new">选择已有模型</a>'
        else:
            body += '<p class="mut">可以选择随包的公开日线入门模型，再准备匹配行情；也可以使用本地 LightGBM / Ridge 五日模型。</p><a class="btn secondary" href="/strategies/new">选择入门或已有模型</a>'
        body += '</article></section><section class="experience-panel"><h2>每一步都能深入</h2><div class="experience-depth"><p><b>先跑起来</b><span>核心选择 + 默认值，随时知道下一步。</span></p><p><b>再做微调</b><span>查参数、看改动和生效条件；切换显示不改变计算。</span></p><p><b>留下证据</b><span>同一配置用于检验与运行，修改生成新版本，旧记录保留。</span></p></div></section>'
        body += '<details><summary>数据、模型和扩展能力</summary><p>可训练本地模型，或选择随包的 40 股入门模型；行情须另行准备。外部预测可通过 CSV 向导声明口径并持续追加；统一插件安装仍在规划中。</p><p><a href="/signals/import">导入外部预测</a> · <a href="/models">本地模型</a> · <a href="/audit">外部预测体检</a> · <a href="/policy-schema.json">政策参数格式</a></p></details>'
        return page("工作台", body)
