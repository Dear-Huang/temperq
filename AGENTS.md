# TemperQ Local Collaboration

TemperQ now opens with the imported-strategy audit workspace. The four pages
share a strategy selector. Uploads, consistency checks, supplements and parameter
confirmation belong to the dedicated strategy import page; My Strategies has
four shortcuts for audit, attribution, comparison and simulation preparation.
Governance selects the strategy in the page body and presents evidence-based
pass/insufficient/adjust conclusions. Limited BigQuant governance patches can be
downloaded as separate code candidates; uploaded code is never executed, and
candidates require a fresh backtest on the source platform.
A1 input checks and A2 period eligibility/internal
partitions are implemented under `docs/specs/STRATEGY_AUDIT_INPUT_SPEC.md`.
The existing account-native review remains at `/review`: user forecasts,
holdings and rules produce reference-price proposals and explicit comparisons.
Its implementation and boundaries remain in `docs/GOVERNANCE_REVIEW.md`.
Training and existing paper-account workflows remain available. Existing release
and research checks in `docs/specs/RELEASE_AND_ADAPTATION_SPEC.md` (v0.2) still
apply to those workflows; a saved review proposal never changes a live ledger.

## Setup And Checks

- Install with `install.bat` on Windows, or use the documented `uv` setup.
- Run `temperq doctor` to check the installed environment.
- Start `temperq ui --no-browser --port 8765`; use another port if occupied.
- Run focused tests with `python -m pytest`, and repository checks with
  `python scripts/check_repository.py`.
- Existing local data and dirty worktree files belong to the user. Preserve them.

## Rule Files

- Schema and commands: `docs/RULE_CARDS.md`.
- Example: `examples/rule_card_v1.json`.
- Read-only check: `temperq rule validate examples/rule_card_v1.json --json`.
- Produce rule JSON and explanations. Let the user import, review the generated
  descriptions and differences, and confirm in the workbench. Do not bypass this
  confirmation or edit strategy files directly to activate rules.
- Imported origin is always `imported`. Validation does not run a backtest or
  count an attempt; evaluation counts regardless of who suggested the rules.

## Boundaries

- Do not alter locked market/timing rules or fees to make a result look better.
- Do not fabricate data sources, history, forward records or performance claims.
- Never echo or commit tokens, keys, holdings or private data paths.
- Do not directly modify user data, delete records, start a user paper account
  or enable daily automation on the user's behalf.
- When explicitly authorized to develop or test, use isolated synthetic test
  workspaces for account lifecycle tests. Never use the user's real accounts.
- No agent runtime, MCP service or external model API is required for 1.0.
