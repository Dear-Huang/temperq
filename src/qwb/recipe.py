"""Data recipe (SPEC §4.6): a shareable description of *what* data to pull.

Recipes travel with model packages; raw data follows its license.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from .asof import POLICIES
from .formats import MARKET_TZ
from .issues import ERROR, WARN, Report

KNOWN_SOURCES = {"tushare", "csv"}
FREQS = {"monthly", "weekly", "daily", "minute", "tick"}
UNIVERSE_TYPES = {"all", "status", "index", "list"}
# Universes defined by a state that changes over time need history to rebuild.
TIME_VARYING = {"status", "index"}


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate(recipe: dict) -> Report:
    rep = Report()
    for key in ("source", "market", "universe", "fields", "start", "freq"):
        if key not in recipe:
            rep.add(ERROR, "MISSING_FIELD", f"recipe needs `{key}`")
    if not rep.ok:
        return rep

    if recipe["source"] not in KNOWN_SOURCES:
        rep.add(WARN, "UNKNOWN_SOURCE",
                f"source {recipe['source']!r} has no built-in connector; a plugin must provide it")
    if recipe["market"] not in MARKET_TZ:
        rep.add(ERROR, "BAD_MARKET", f"market must be one of {sorted(MARKET_TZ)}")
    if recipe["freq"] not in FREQS:
        rep.add(ERROR, "BAD_FREQ", f"freq must be one of {sorted(FREQS)}")
    if not isinstance(recipe["fields"], list) or not recipe["fields"]:
        rep.add(ERROR, "NO_FIELDS", "fields must be a non-empty list")
    if recipe.get("revision_policy", "latest") not in POLICIES:
        rep.add(ERROR, "BAD_POLICY", f"revision_policy must be one of {sorted(POLICIES)}")

    start = pd.to_datetime(recipe["start"], errors="coerce")
    if pd.isna(start):
        rep.add(ERROR, "BAD_TIME", "start is not a date")
    if recipe.get("end") is not None:
        end = pd.to_datetime(recipe["end"], errors="coerce")
        if pd.isna(end) or (not pd.isna(start) and end <= start):
            rep.add(ERROR, "BAD_TIME", "end must be a date after start")

    uni = recipe["universe"]
    utype = uni.get("type") if isinstance(uni, dict) else None
    if utype not in UNIVERSE_TYPES:
        rep.add(ERROR, "BAD_UNIVERSE", f"universe.type must be one of {sorted(UNIVERSE_TYPES)}")
    elif utype in TIME_VARYING and uni.get("point_in_time") is not True:
        # Using today's ST flag / index list for history is look-ahead.
        level = WARN if recipe.get("experimental") else ERROR
        rep.add(level, "LOOKAHEAD_UNIVERSE",
                f"universe.type={utype} must set point_in_time: true "
                "(or mark the recipe experimental: true)")
    return rep
