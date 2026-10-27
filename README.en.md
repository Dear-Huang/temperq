<div align="center">

# TemperQ

**How much of your backtest profit is real?**

Open source · Runs locally · Never executes your code · BigQuant / JoinQuant / RiceQuant

English · [中文](README.md)

[![CI](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml/badge.svg)](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)

[Quick start](#quick-start) · [Import format](docs/STRATEGY_AUDIT_FORMAT.md) · [Feedback](https://github.com/Dear-Huang/temperq/issues)

</div>

TemperQ is a **local backtest auditor** for daily China A-share strategies. Build and backtest your strategy on BigQuant, JoinQuant or RiceQuant, drop in the platform's raw exports, and TemperQ reconciles the account fill by fill. It tells you whether fees were charged correctly, how much the bid-ask spread costs, which stocks made the money, and what to change and whether the change is worth it.

It audits the backtest **output** only. It does not run your strategy code and does not replace the platform you use.

<p align="center">
  <img src="docs/img/audit-governance.png" width="100%" alt="TemperQ strategy governance: account reconciliation, findings and suggested changes">
</p>

<p align="center"><sub>Strategy governance page on the built-in synthetic sample. Illustrates the interface, not investment performance.</sub></p>

The interface and user documentation are currently in Chinese.

## Three pages, one backtest

| Page | Question | What you get |
| --- | --- | --- |
| **Strategy governance** | What is wrong and what to change | One card per issue: finding, options (always including "keep as is"), what it saves, what it may give up, evidence level, and what would overturn it |
| **P&L attribution** | Where the money came from | A waterfall from gross P&L through commissions and stamp tax to net P&L and the spread scenario; concentration by stock, holding period and price band; who caused the maximum drawdown |
| **Effect verification** | Is the changed version actually better | First restates the original with realistic costs; then compares an optimized re-run, exported from the original platform, on the full period and an internal validation segment: better / mixed / worse |

<p align="center">
  <img src="docs/img/audit-attribution.png" width="100%" alt="TemperQ P&L attribution waterfall from gross P&L to after-spread P&L">
</p>

## Why the audit can be trusted

- **Reconcile first, then conclude.** Every day: equity = cash + market value, position changes = fills, cash changes = traded amounts + fees, and P&L bridges day to day. Modules whose books do not close are marked "needs reconciliation" and draw no conclusion.
- **Fees recomputed with the rules of the day.** Commission, minimum commission and stamp tax are recomputed at the rate in force on each trade date and compared fill by fill with what the platform charged.
- **A spread floor anyone can check.** The A-share tick is 0.01 yuan, so each side pays at least half a tick: `half spread (bp) = 10000 × 0.01 / (2 × price)`. For low-priced, high-turnover strategies this often exceeds commission. It is a scenario, not measured execution cost, and the page says so.
- **Every inference is labelled.** With holdings but no fills, trades are inferred from holdings. JoinQuant and RiceQuant do not export dividends or bonus shares; TemperQ recovers them from cash and share changes and lists them as "inferred from the account" for you to confirm.
- **Changes can be tested.** For BigQuant strategies TemperQ can generate optimized code. Re-run it on the platform, upload the result, and both versions are measured with the same ruler.

## Supported platforms

| Platform | Upload | Coverage |
| --- | --- | --- |
| **BigQuant** | Trades, holdings and log exports + strategy code | Full audit; optimized code |
| **JoinQuant** | Transaction and position exports + strategy code | Full audit; dividends and bonus shares inferred; written suggestions |
| **RiceQuant** | Trade, position and account detail exports + strategy code | Full audit; dividends and bonus shares inferred; written suggestions |
| **Others** | trades / holdings / account in the [generic format](docs/STRATEGY_AUDIT_FORMAT.md) | Fees, spread, attribution; only obvious code parameters are read |

Upload the raw files **unchanged**; the format is detected from table headers. Files from different platforms cannot be mixed in one backtest. All three platforms have been reconciled end to end on real backtest exports.

Daily stock scores are optional. None of the three platforms makes them easy to export, and no 1.0 audit depends on them. When provided, they add an analysis of how long the signal lasts.

## Privacy and safety

- Strategy code is parsed **read-only** with the Python AST. It is never imported or executed.
- All files and reports stay on your computer and are not uploaded anywhere.
- The workbench listens on the local address (`127.0.0.1`) only.

## Quick start

Runs on **Windows 10/11 and Linux** with CPU only; 8GB+ RAM recommended. The first install needs network access to prepare an isolated Python 3.11 environment.

### Windows

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and reopen the terminal.
2. [Download the source ZIP](https://github.com/Dear-Huang/temperq/archive/refs/heads/main.zip) and extract it.
3. Double-click `install.bat` and wait for the self-check.
4. Double-click `start.bat`; the local workbench opens in your browser. Keep the window open while using it.

<details>
<summary>Install with Git / Linux</summary>

**Windows** (uv and Git required):

```powershell
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
.\install.bat
.\start.bat
```

**Linux** (Git and curl required; uv is installed if missing):

```bash
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
sh install.sh
sh start.sh
```

The default address is `http://127.0.0.1:8765`; the startup window shows the actual one.

</details>

### First run

1. Open "策略导入" (import), download the synthetic sample and upload it as is to walk through the three pages.
2. Open a backtest on your platform, export the files listed above and drop them in with the strategy code.
3. On the review page, check what was recognized and inferred, confirm the files come from the same run, and continue.
4. Apply the suggested changes, re-run on the original platform, and upload the optimized version under "效果验证" (verification).

## Scope and limits

- The spread scenario counts only the tick-size floor. **Market impact of large orders is not yet included**; for large accounts trading small caps, real costs will be higher.
- Inferred dividends are allocated by the previous day's market value and are estimates; platform corporate-action records take precedence when available.
- Attribution and verification describe what happened in the historical backtest and do not forecast returns. This project is for research and learning and is not investment advice.

<details>
<summary>Retained features: model training, account review and paper tracking</summary>

Visual model training (LightGBM / Ridge), account adjustment review, historical replay and local paper accounts from earlier versions remain available from the workbench settings. See the [strategy workspace guide](docs/STRATEGY_WORKSPACE.md) and the [governance guide](docs/GOVERNANCE_REVIEW.md).

</details>

## Documentation and contributing

- [Strategy audit import format](docs/STRATEGY_AUDIT_FORMAT.md): exports per platform, generic fields and coverage.
- [Why this design](docs/WHY_THIS_DESIGN.md): why audit outputs, never run code and not rank returns.
- [Pitfalls](docs/PITFALLS.md): look-ahead, overnight gaps, mistaking forecast metrics for returns.
- [Development](docs/DEVELOPMENT.md): project structure, tests and environment.

TemperQ is at an early stage. [Issues and requests](https://github.com/Dear-Huang/temperq/issues) are welcome. If your platform is not supported yet, attach a redacted export header and it will be prioritized.

## License

Code is licensed under [AGPL-3.0](LICENSE), copyright Dear-Huang. Third-party components follow their own licenses; see [NOTICE](NOTICE).
