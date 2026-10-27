# Strategy Import And Governance Audit

The strategy audit workspace is available at `/strategy-audits`. Import the
trade and daily holdings exports from one BigQuant backtest, optionally with
strategy code, logs, the platform attribution table and daily quoted-spread
comparison CSV. Confirm the recognized
materials and audit settings, then run the report. Existing account planning,
paper-account routes remain available. Training and model-library entries are
hidden from the audit workspace and navigation; historical code and records are retained.
The report's 15-stage coverage and source mapping are documented in
[STRATEGY_AUDIT_COVERAGE.md](STRATEGY_AUDIT_COVERAGE.md).

## Implemented Workflow

1. Import UTF-8 CSV and TXT/PY files in one batch. Detection uses table headers,
   not filenames. Dates with merged empty cells are filled in original export
   order before sorting. Summary rows do not become trades or positions.
2. Review the recognized files, dates, counts and literal code parameters.
   Uploaded code is parsed with Python AST only. It is never imported or run.
   Literals in unused functions or branches are not assumed to be active rules.
3. Confirm fee-scenario and closing-position-cap settings. These values are
   separate from the reported transaction fees. Repeated analyses append reports.
4. Inspect inventory, cash, valuation and P&L reconciliation; per-stock net/gross
   contributions; drawdown-window stock/fee contributions; HHI, effective stock
   count and cash exposure; turnover and same-day two-way activity; per-fill
   fee reconciliation and conditional batching. Download JSON or CSV evidence.
5. Carry the final historical holdings into the existing account planner. All
   sellable quantities start at zero, quote statuses are conditional, the next
   execution date, account/signal timestamps and predictions are blank. Update and confirm current account
   information before calculating proposals. This never creates fills or changes
   an account ledger.

The downloadable sample ZIP is synthetic. Its initial empty account has cash
10,000. Enter this value and confirm empty opening to include the first day.
The sample closes one position and retains another, ending at 10,183.90.

## Accounting Scope

With a user-confirmed empty opening and cash amount, the first exported date
is included. Otherwise the first close is the opening baseline and its trades
are excluded from interval P&L. External deposits/withdrawals are not inferred.
They appear as cash reconciliation exceptions. This version requires daily
account totals in the holdings export. It does not certify the completeness of
the supplied export or a full live-account company-action lifecycle.

For each date and security:

```text
gross contribution = ending market value - beginning market value
                     - net purchase notional + recorded action cash
net contribution   = gross contribution - reported transaction fees
```

This includes closed holdings and unrealized P&L. The sum is compared with
account equity changes. Absolute monetary tolerance is 0.05; quantity differences
have zero tolerance. A failure leaves attribution explicitly provisional.
Corporate actions are read only from the supported `split[...]` log format;
missing events produce inventory/cash exceptions. No adjustments are invented.

Positive-contribution concentration uses the sum of positive contributions as
its denominator. It does not divide by small net profit. Removing a recorded
stock contribution is accounting sensitivity, not a rerun excluding that stock.
Accounting contributions cannot establish skill, luck or predictive alpha.
Daily monetary contributions and initial-capital basis points are additive;
they are not geometrically linked returns.

The external industry attribution CSV is preserved separately as unverified.
It needs matching dates, units and methodology before it can be reconciled.

## Extension Boundaries

The implementation has seven small ownership boundaries:

| Module | Responsibility |
| --- | --- |
| `strategy_imports.py` | Source limits, adapter selection, normalization, static literals |
| `strategy_audit.py` | Accounting, attribution and audit orchestration |
| `audit_diagnostics.py` | Fee reconciliation, conditional batching, cost stress, concentration and drawdown contributions |
| `audit_research.py` | STRUCT tick-component stress, daily quote comparison and censored FIFO holding/entry-price attribution |
| `audit_catalog.py` | Ordered audit stages, observed metrics, candidate actions and evidence boundaries |
| `strategy_audit_store.py` | Immutable imports, append-only reports, digest verification |
| `strategy_audit_ui.py` | Upload, material confirmation, report, export, planner handoff |

Normalized imports use `strategy_export_v1`, with source hashes and row references.
Tables are `fills`, `positions`, `snapshots`, `actions`, `logs` and
`external_attribution`; `opening` declares the accounting baseline. A new platform
adapter returns this contract and is registered in `ADAPTERS`. Reports use
`strategy_audit_v1`, with independently versioned `accounting`, `attribution` and
`governance`, `fee_analysis`, `risk_analysis` and `trading_behavior` modules. Input and result digests link a report to its materials and
settings. Stored source copies are verified on read. New reports include
`coverage_version: audit_coverage_v3` and a `coverage` list with all 15 stages.
Each stage lists methods with independent implementation and input states. A
working fee checker does not imply a working STRUCT adapter; an available account
planner still requires current targets and sellability confirmation. Old reports
are displayed without recomputing diagnostics or rewriting stored evidence. Rerun
analysis to append a new report with the current methods.

## Connected Diagnostic Methods

- **Per-fill fees:** reuse `portfolio.reference.Fees` and the frozen dated stamp-tax
  pack. Show actual fees, modeled commission/tax, signed differences and absolute
  differences (opposing discrepancies must not hide one another). Differences over
  0.02 are review items. Fees absent from the configured model are not invented.
  Unsupported tax dates stay unpriced. Actual reported fees and P&L are unchanged.
- **Conditional batching:** only identical reported date, clock, security and side
  can form a candidate. Compare separate minimum commissions with one commission
  on the combined notional, preserving quantities, prices and taxes. Report the
  adverse-price budget in bp that would erase the commission reduction. Order
  identity and charging basis must be confirmed: multiple fills might already
  belong to one order. This is neither an order instruction nor achieved savings.
  Missing clocks are excluded; cross-day grouping and buy/sell netting are forbidden.
- **Cost robustness:** on the accounting intervals only, keep fills fixed and add
  5/10bp on both purchase and sale notional. Show the extra per-side bp that would
  exhaust current positive net P&L (zero if already nonpositive). This stress does
  not replay altered cash balances or subsequent orders; unclosed accounts stay
  provisional. First-close baseline fills cannot enter the later P&L denominator.
- **Risk and drawdown:** peak-to-trough percentage drawdown on exported equity
  snapshots, with the declared opening included only when known. Attribute the
  selected peak-to-trough monetary loss to stock gross P&L and reported fees and
  retain the reconciliation residual and recovery date. No annualization or claim
  of complete trading-day coverage. Effective stock count is `1/sum(w_stock^2)`
  with weights normalized inside the invested stock sleeve; cash is shown separately.
- **Cap repair:** sum `max(marked_position_value - cap * equity, 0)` at the final
  snapshot. This is a necessary marked-sale lower bound before fees at fixed
  prices, not a feasible transaction plan. Confirm current targets, sellability,
  fees and trade blocks in the existing SciPy/HiGHS integer planner. A report
  handoff carries the confirmed cap into the editable form, never activates it.
- **Two-way activity:** same-security/day monetary overlap is descriptive, not a
  verdict of waste or a T+1 violation. Selling old inventory and rebuying may be
  intentional. Complete orders and targets are required to explain the activity.

Additional CSVs are `fees.csv` (per-fill model and gaps) and `risk.csv` (snapshot
concentration/cash metrics); all group row references and drawdown contributions
are in `evidence.json`. These standard methods are not claimed as novel or
universally optimal. The product combines evidence and account constraints;
historically optimal governance still requires a separate controlled replay.

## Tick Component, Holding Periods And Quote Evidence

Reports include `tick_analysis` and `holding_analysis` (version 1). The tick
component calls the existing `ashare_tcost.tick_floor_bp` through
`struct_costs.tick_half_spread_bp`: full quoted tick spread is `100/price` bp;
the per-side scenario is half that. At the imported fill price its monetary
cost is `notional * 50/price/10000` (0.005 yuan per share when the notional
identity holds). It is additional cost stress only if the original result
does not already contain spread/slippage. Reported fees and tax gaps are never
rewritten or simultaneously credited as savings.

All exported fills and accounting intervals are distinguished, so first-close
baseline fills cannot enter later P&L stress. Four price bands (under 5, 5 to
under 10, 10 to under 20, 20+) show traded-notional exposure, reported fees and
tick scenario costs. This is **not a realized cost floor**: fill price substitutes
approximately for midpoint, while auctions, passive orders and continuous
execution have different economics. Negative stressed P&L does not establish
future loss. The full STRUCT liquidity component remains outside this report.

FIFO matches prior-day bought inventory to observed later sales, allocating
entry/exit fees by shares. Same-day buys cannot cover an invalid sale. Buys use
reported clocks when all are valid, otherwise source order; ties preserve source
order. Calendar days and exported snapshot intervals are distinct, and neither
implies a complete trading calendar. Open lots are marked at the final reported
price and right-censored, with no invented sale, exit fee or exit spread. Entry
fees and tick costs split between closed and remaining shares without duplication.

This is **observed FIFO accounting attribution**, not the signal age experiment
in 093459. In the examined code, `hold_days=2` governs staging and cash budgeting;
it is not a forced two-day exit rule. Group differences are selected ex post and
cannot establish that a 5/10-day hold improves performance. Signal-age measurement
still needs frozen timestamped predictions; policy replay needs the candidate
universe's prices, tradability and company actions. Neither is newly claimed.

Unknown opening lots have no fabricated entry costs or ages. Securities with
nonzero company actions, inventory violations or valuation/notional inconsistencies
are excluded from this first lot matcher, remaining fully included in ordinary
stock attribution. Excluded fill count/notional, unknown quantities, unmatched
inventory and open lots are explicit. For a known empty opening, closed plus
open P&L is bridged against only covered securities' account contributions.
Covered-stock results cannot represent the entire strategy. Entry-price profit
groups differ from the fill-price cost groups; they are not silently joined.

Optional `quoted_spreads.csv` has these columns:

```csv
date,code,quoted_spread_bp,sample_count,measurement_scope
2024-01-02,600000.SH,10,100,daily_quote_mean
```

`quoted_spread_bp` is positive **full** quoted spread in bp, at most 10,000;
`sample_count` is a positive integer. Only `daily_quote_mean` is supported.
Duplicate security/date records fail validation. Matching is same-security/date
only, without filling missing coverage. Measured and proxy half spreads use the
same matched fill-notional weights. Values below 98% of the fill-price proxy
are reported without clipping: daily prices, auction/crossed quotes or cleaning
choices can explain differences. Daily means do not certify trade-time or
auction execution costs, and source cleaning is not independently certified.

`scripts/validate_audit_tick_quotes.py --exports <export-directory> --l2-daily
<daily-warehouse> --output <new-directory> [--opening-cash <cash>]` reads only dates
in imported fills. Supported layout is `date=YYYYMMDD/part.parquet`, with columns
`trade_date`, `wind_code`, `相对价差_均` (decimal full spread), `快照数`.
It writes the optional CSV and hashes, coverage and source-quality gaps to a new
directory; overwriting is refused. Default minimum snapshots is 10, explicitly
configurable. It never opens unrelated dates or fits/evaluates prediction models.

Additional exports are `tick-costs.csv` and `holding-lots.csv`. JSON contains all
source row references and matched quote/lot evidence. Old reports never acquire
invented computations; rerun analysis to append a new report.

Historical optimization requires complete, timestamped forecasts, the candidate
universe's historical prices/tradability and explicit construction rules. Factor
attribution requires factor returns and point-in-time exposures. Signal-age
analysis requires timestamped signal histories. These are capability requirements,
not simulated or completed algorithms. Prediction training is outside the current
audit interface and is not part of the coverage catalog.

The first fee scenario measures the difference between the confirmed commission
schedule with and without its minimum, keeping fills fixed. This is a friction
amount, not achievable savings. Candidate actions are merging small adjustments,
partial adjustment and no-trade bands. They are not ranked as historically optimal
until complete policy replay is implemented and independently reconciled. Frozen
market rules, taxes, settlement and trading blocks remain hard constraints.

## Storage And Limits

Imports and reports live under ignored `runs/strategy_audits`. User originals
are never modified. Source filenames cannot contain path separators; source
copies use generated internal filenames. Maximum six files, 8 MB per file,
24 MB combined; code is limited to 200 KB and tables to 50,000 records each.
The multipart request is bounded before full parsing. Mutating routes require
the workbench token and allowed local Host. HTML values are escaped; CSV exports
contain normalized measurements and optional user-declared order/fee references.
Order identifiers and contract references in CSV downloads are escaped as literal
spreadsheet cells; the original text remains in immutable input/evidence JSON.
No new third-party runtime dependencies or external model services are required.

## Order Fee Units (2026-10-27)

Optional order/fill identifiers, a declared commission basis, a contract/settlement
reference and final-order export completeness allow aggregate fee reconciliation.
Partial fills of one order are not independent orders to merge. Unknown charging
bases remain scenarios and cannot by themselves trigger a fee discrepancy verdict.
The source of the declaration is the user; this does not certify a broker contract.
See the [research implementation note](research/TEMPERQ_EXTERNAL_AUDIT_20261027/11_研究落地增量_订单计费与受阻目标.md)
for the field contract, economic examples and verification boundaries.
