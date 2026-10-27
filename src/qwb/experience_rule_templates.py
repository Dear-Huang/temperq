"""Compatibility API for the versioned daily rule catalog."""
from . import rule_cards


def catalog():
    return {doc["id"]: {"id": doc["id"], "name": doc["name"],
            "rules": doc["rules"], "data": "daily_ohlc_amount",
            "scope": "starter_scorecard_not_performance_claim"} for doc in rule_cards.catalog()}


def get(template_id):
    return rule_cards.scorecard(rule_cards.template(template_id))
