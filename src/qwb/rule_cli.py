"""Read-only rule validation; never imports a strategy or starts an account."""
import json
from pathlib import Path

from . import rule_cards


def add_parser(sub):
    parser = sub.add_parser("rule", help="validate a rule_card_v1 JSON file (read-only)")
    commands = parser.add_subparsers(dest="rule_cmd", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--json", action="store_true")


def run(args):
    try:
        with Path(args.path).open("rb") as stream:
            doc = rule_cards.loads(stream.read(rule_cards.MAX_BYTES + 1))
        result = rule_cards.report(doc)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {"valid": False, "errors": ["无法读取规则文件" if isinstance(exc, OSError) else str(exc)],
                  "rules": [], "required_fields": []}
    if args.json: print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        print("规则有效" if result["valid"] else "；".join(result["errors"]))
        for rule in result["rules"]: print(rule["description"])
    return 0 if result["valid"] else 1
