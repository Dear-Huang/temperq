<div align="center">

# TemperQ

**Would your backtest survive an audit?**

Hand TemperQ the files your backtest platform exports and get back an audit report:<br>
can the return be trusted, was your idea right, and what to change.

Open source · Runs locally · Never executes your code · BigQuant / JoinQuant / RiceQuant, other platforms via a standard format

English · [中文](README.md)

[![CI](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml/badge.svg)](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)

[Quick start](#quick-start) · [What it finds](#what-it-finds) · [Import format](docs/STRATEGY_AUDIT_FORMAT.md) · [Feedback](https://github.com/Dear-Huang/temperq/issues)

</div>

The equity curve looks great, but do you know where it came from? Were the fees charged right? Could you really have filled at that price in the 9:30 minute? Did you make money because of your factor, or because everything in the pool went up?

TemperQ looks only at the backtest's **outputs**: fills, holdings, account, logs and strategy code. It reconciles the books day by day, audits each stage, and writes a report you can recompute. Your code is read, never run.

TemperQ targets China A-shares (stocks, ETFs and convertible bonds, long only). Reports are in Chinese.

## What it finds

These are real results: public JoinQuant community strategies, migrated to a local RiceQuant (RQAlpha) install, backtested, then audited by TemperQ.

| You think | The audit finds |
| --- | --- |
| It made ¥4,318 in a year, so the strategy works | Moving the fill to another minute of the same day changes P&L by −¥736 to +¥9,215, more than the profit itself: **the conclusion depends on when you assume it filled** |
| The momentum factor is doing the work | The candidate pool has 4 ETFs; at the observed effect size it would take about 9 years of data to detect it: **not detected is not wrong, but this backtest cannot show it** |
| Keep 1% cash and add a retention band, two improvements at once | 1% cash alone adds 0.71 percentage points; together with the band it costs 3.80: **the two suggestions do not add up** |
| It bought the target ETF, so execution was fine | One buy filled 30%, then 71 trading days with no top-up while 67% of equity sat in cash: **holding the target is not completing the order** |
| 219 days not holding the rank-1 name means execution failed | 193 of them were the strategy's own retention rule; the real execution gaps were 26 days: **the strategy's design is not mistaken for a fault** |
| 108.8% in three years, every switch went smoothly | 43 switches could only pay for the buy with the sale made in the same minute; the backtest assumed the sale filled first and the cash arrived at once. Re-run with next-bar matching, the dependence disappears and the return becomes 87.1%: **part of the return came from ideal funding between legs** |

For 10 RiceQuant one-year backtests, the audit's recomputed total return, maximum drawdown and benchmark return match RiceQuant's own figures (the Sharpe ratio differs because RiceQuant subtracts a risk-free rate; the report says so).

## What the report looks like

The report opens with three questions:

<table>
<tr>
<td width="33%" valign="top"><b>Can the return be trusted?</b><br><sub>Do the books close, does it match the platform's own figures, can a claimed return be reproduced, what if fills happened at another minute, is it still profitable after the bid-ask spread</sub></td>
<td width="33%" valign="top"><b>Was your idea right?</b><br><sub>The factor your strategy relies on, read from the code and its picks, tested: right or not, by how much, in which periods. When not detected, it says whether the sample was too small or the idea did not hold</sub></td>
<td width="33%" valign="top"><b>What to change</b><br><sub>At most three changes, each pointing to a line of code, with what it saves, what it gives up, and when the suggestion would not hold</sub></td>
</tr>
</table>

Seven sections follow: conclusions, premise check, return and risk (with the platform's own figures alongside), P&L attribution, costs and execution, findings and suggestions, scope and limits. The report lists **which checks ran, which could not run for missing materials and what would make them possible**. Not checked does not mean no problem, and the report never pretends otherwise.

<p align="center">
  <img src="docs/img/audit-attribution.png" width="100%" alt="TemperQ P&L attribution: from gross P&L to P&L after the spread scenario">
</p>
<p align="center"><sub>P&L attribution page, built-in synthetic sample. Interface only; not investment performance.</sub></p>

## Why the report can be trusted

- **Books first, conclusions second.** Each day: equity = cash + market value, change in holdings = fills, change in cash = traded value + fees. A stage whose books do not close is marked "to reconcile" and gets no conclusion.
- **Every number can be recomputed.** The report ships with all inputs, intermediate results and per-trade detail, and the hashes of input and result. The same materials always give the same report.
- **Every judgement has a source.** Exchange rules, tax rates, statistical methods and rule-of-thumb thresholds are registered separately; each suggestion cites the ones it rests on. Thresholds are labelled "not tested on samples" and can be changed. See [audit rules](docs/AUDIT_RULES.md).
- **Evidence is graded.** Recomputed records, after-the-fact description, scenario estimates and same-period re-runs are labelled as such. A scenario is not a fact, and in-sample is not out-of-sample.
- **The report is not written by an AI.** Even in the AI version, the audit engine generates the report; the AI only converts formats and relays the result, and may not add its own judgement.

## Two ways to use it

| | Local web app | AI version (skill) |
| --- | --- | --- |
| For | Nothing may leave your computer | Messy materials you would rather not reformat |
| Materials | Drop in BigQuant, JoinQuant or RiceQuant exports as they are; other platforms in the [standard format](docs/STRATEGY_AUDIT_FORMAT.md) | Anything; the AI reads the headers and writes a conversion script |
| Who writes the report | The audit engine | The same engine; the report is identical |
| Where data goes | Stays on your machine | The audit runs locally; file headers and a few sample rows are sent to the model |

The AI version lives in [`skills/strategy-audit/`](skills/strategy-audit/SKILL.md). After converting, the AI must reconcile the books with the engine first; if they do not close, the conversion is wrong and has to be redone.

To use it in Claude Code: install TemperQ as in the quick start below, copy the whole `skills/strategy-audit` folder to `~/.claude/skills/` (all projects) or your project's `.claude/skills/`, then ask Claude to audit your backtest and point it to the folder with the exported files.

## Want more checks? Add two materials

Platforms do not export these by default. With them, two more checks run:

- **Daily scores** (does your factor work): paste [`temperq_recorder.py`](src/qwb/static/temperq_recorder.py) into a copy of the strategy, add one line `temperq_record(context, scores, selected)`, and re-run once on the platform. `qwb audit instrument-check` proves the copy differs from the original only by the recording lines.
- **Candidate closes and minute prices on fill days** (factor and fill-time checks): run [`temperq_supplement.py`](src/qwb/static/temperq_supplement.py) in the platform's research environment.

## Was the change worth it

After the report suggests a change, edit the code, re-run on the platform and audit the new export:

- `qwb audit compare <original> <changed>`: compares the two versions by the same method and says better, mixed or worse. When both versions made exactly the same trades, it says the difference comes from fees alone.
- `qwb audit interaction <base> <only A> <only B> <both>`: whether two suggestions can be used together.

A same-period re-run is still in-sample, and the report says so.

## Quick start

**Windows 10/11 and Linux**, CPU only, 8 GB RAM or more recommended. The first install needs internet; the script sets up its own Python 3.11 and leaves any Python you already have alone.

### Windows

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then reopen the terminal.
2. [Download the source ZIP](https://github.com/Dear-Huang/temperq/archive/refs/heads/main.zip) and extract it fully.
3. Double-click `install.bat` and wait for the install and self-check.
4. Double-click `start.bat`; the browser opens the local workbench. Under 策略导入 (strategy import), 下载合成示例 (download the synthetic sample) lets you try the full flow first.

### Command line

```powershell
.venv\Scripts\qwb audit check D:\my_backtest_export                    # what was read, do the books close
.venv\Scripts\qwb audit run   D:\my_backtest_export --out D:\audit_out  # writes report.html and more
```

Open `report.html` in a browser; it prints to PDF. Options include `--commission-bp` (broker contract commission), `--premise-text` (your own statement of the strategy's premise) and `--claimed-total` (a claimed total return to compare against); see `qwb audit run --help`.

<details>
<summary>Install with Git / Linux</summary>

**Windows** (install uv and Git first):

```powershell
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
.\install.bat
.\start.bat
```

**Linux** (needs Git and curl; the script installs uv if missing):

```bash
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
sh install.sh
sh start.sh
```

The default address is `http://127.0.0.1:8765`; the start window shows the actual one. Troubleshooting: [user manual](docs/使用手册.md) (Chinese).

</details>

## Supported platforms

| Platform | What to provide |
| --- | --- |
| BigQuant | Trades, holdings and log from the backtest result page, plus the strategy code, as exported |
| JoinQuant | Trade details and holdings, plus the strategy code; ZIP files need not be extracted |
| RiceQuant | Web exports of trades, holdings and account, or a local RQAlpha native report, plus the strategy code |
| Myquant, QMT, PTrade, in-house backtesters | Three CSVs in the [standard format](docs/STRATEGY_AUDIT_FORMAT.md) (trades, holdings, account), or let the AI version convert them |

China A-share stocks, ETFs/funds and convertible bonds, long only. Futures, options and the Beijing Stock Exchange are not supported yet.

## Privacy and safety

- Strategy code is parsed **read-only** with Python's AST; it is never imported or executed.
- In the local web app, all files and reports stay on your computer and are never uploaded.
- The workbench listens on the local address only (`127.0.0.1`).

## Scope and limits

- The bid-ask spread is a lower bound from the minimum price tick; **market impact of large orders is not included yet**. Large accounts trading small caps will pay more.
- The fill-time check changes only the fill price; it does not re-run the strategy's decisions. Inferred dividends and splits are listed for you to confirm.
- Every conclusion describes this historical backtest only and does not predict future returns. For research and learning; not investment advice.

<details>
<summary>Other retained features: model training, account diagnostics and paper tracking</summary>

The visual training (LightGBM / Ridge), account adjustment diagnostics, historical replay and local paper account from earlier versions remain available from the workbench settings. See the [strategy workspace guide](docs/STRATEGY_WORKSPACE.md) and the [governance workbench guide](docs/GOVERNANCE_REVIEW.md).

</details>

## Docs and contributing

- [Strategy audit import format](docs/STRATEGY_AUDIT_FORMAT.md): how to export from each platform, standard format fields.
- [Audit rules](docs/AUDIT_RULES.md): the source of every rule, method and threshold.
- [Why this design](docs/WHY_THIS_DESIGN.md): why audit outputs, never run code, and not compete on returns.
- [Pitfalls](docs/PITFALLS.md): look-ahead, overnight gaps, mistaking prediction metrics for returns, and more.
- [Development](docs/DEVELOPMENT.md): project layout, tests and environment.

TemperQ is at an early stage. Try it on one of your own backtests. If it finds a problem, if the report is hard to read, or if you think a judgement is wrong, please [open an issue](https://github.com/Dear-Huang/temperq/issues). If your platform is not supported yet, attach the headers of an anonymised export and we will prioritise it.

## Recent changes (October 2026)

This round came from repeated independent reviews: real RiceQuant and JoinQuant strategies were audited end to end, and a second party checked the report line by line. The changes share one theme: **the audit no longer treats its own inferences as facts**. It uses explicit materials when given, and says what it could not read when not.

New checks:

- **Does the return belong to the declared strategy**: each security-day's P&L is grouped by whether the name was inside the strategy's target at the previous close. In one rotation case, 37% of net profit came from holdings already outside the target. This is an accounting split; it is not subtracted from the return and does not predict a fixed version.
- **Overnight exposure**: days still holding at the close, and the P&L from that close to the next open, so a strategy that claims to close out daily can be checked.
- **Can the capital hold the allocation**: an integer program gives the smallest whole-lot distance from the target weights (an optimistic floor). A floor above the tolerance proves that capital cannot meet it; a small floor proves nothing.
- **Target weight fulfilment**: optional `target_weights.csv`, comparing closing weights with targets and marking gaps within one board lot.
- **Buying the top name, tested on its own**: besides the full-rank IC, the return of the top-ranked name against the candidate pool, with its t statistic. They answer different questions.
- **Exits that failed, long idle stretches**: sells that did not complete and the days still held after; the longest run with no fills and no holdings (an intraday strategy that ends flat is not idle).

Corrected judgements:

- A day whose scores are all `selected=false` is an empty target, not "the top name by default"; a stated target is never excused by an inferred retention rule.
- A top-up order that filled nothing is not a top-up; long gaps after a partial buy are reported.
- Holdings outside the target, and days the strategy chose cash but still held something, are counted.
- Dividends by recorded shares, no same-day resale for T+1 equity ETFs, platform default fees, and "contributed least" rather than "lost most" when every group made money.

The source of every rule and threshold is in [Audit rules](docs/AUDIT_RULES.md).

## License

Code under [AGPL-3.0](LICENSE), copyright Dear-Huang. Third-party components follow their own licenses; see [NOTICE](NOTICE).
