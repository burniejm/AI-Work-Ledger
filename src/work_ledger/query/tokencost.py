"""Token usage and estimated cost per ticket (DESIGN "Token cost").

Usage comes from `session_tokens` (accumulated exactly once per transcript
record). Cost is estimated from a per-MTok rate table -- shipped defaults
below, overridable via `costs.model_rates` in config.yaml -- and an unknown
model yields token counts with `cost_usd: None`, never a guess.

Attribution honesty (README "Claude usage & cost"): a ticket's numbers sum
its contributing sessions whole, the same granularity as the AI-usage
score, so two tickets sharing one session both show that session's full
usage. Client/day rollups (`tokens_for_sessions`) count each session once,
so the totals a user would actually sum stay correct.
"""

from work_ledger import config

# Anthropic first-party per-MTok rates (cached from the pricing table,
# 2026-06: input / output USD per million tokens). Cache multipliers below
# are the standard published ratios (reads 0.1x input, 5-minute writes
# 1.25x input) -- an approximation for models with promotional cache rates;
# override any of this via `costs.model_rates` in config.yaml. Keys match
# by prefix so dated ids ("claude-haiku-4-5-20251001") find their family.
DEFAULT_RATES = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

CACHE_READ_MULTIPLIER = 0.1
CACHE_CREATION_MULTIPLIER = 1.25


def _rates() -> dict:
    rates = dict(DEFAULT_RATES)
    override = config.get("costs.model_rates", None)
    if isinstance(override, dict):
        for model, entry in override.items():
            if isinstance(entry, dict):
                rates[str(model)] = entry
    return rates


def _rate_for(model: str, rates: dict) -> "dict | None":
    if model in rates:
        return rates[model]
    # Longest-prefix match: "claude-haiku-4-5-20251001" -> "claude-haiku-4-5".
    best = None
    for key in rates:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return rates[best] if best else None


def cost_usd(model: str, tokens: dict) -> "float | None":
    """Estimated USD for one model's token counts, or None when the model
    has no configured rate (counts still shown, cost never guessed)."""
    rate = _rate_for(model, _rates())
    if rate is None:
        return None
    try:
        input_rate = float(rate["input"])
        output_rate = float(rate["output"])
    except (KeyError, TypeError, ValueError):
        return None
    cache_read_rate = rate.get("cache_read", input_rate * CACHE_READ_MULTIPLIER)
    cache_creation_rate = rate.get("cache_creation",
                                   input_rate * CACHE_CREATION_MULTIPLIER)
    per_mtok = (
        tokens.get("input_tokens", 0) * input_rate
        + tokens.get("output_tokens", 0) * output_rate
        + tokens.get("cache_read_tokens", 0) * float(cache_read_rate)
        + tokens.get("cache_creation_tokens", 0) * float(cache_creation_rate)
    )
    return per_mtok / 1_000_000


_ZERO = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _empty() -> dict:
    return {key: 0 for key in _ZERO}


def tokens_for_sessions(conn, session_ids: "list[str]") -> dict:
    """Per-model sums and totals over a set of sessions, each counted once."""
    per_model: dict = {}
    for sid in sorted(set(session_ids)):
        for row in conn.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_tokens,"
                " cache_creation_tokens FROM session_tokens WHERE session_id=?",
                (sid,)):
            bucket = per_model.setdefault(row["model"], _empty())
            for key, column in zip(_ZERO, ("input_tokens", "output_tokens",
                                           "cache_read_tokens", "cache_creation_tokens")):
                bucket[key] += row[column]

    totals = _empty()
    models, total_cost, cost_known = [], 0.0, True
    for model in sorted(per_model):
        tokens = per_model[model]
        for key in _ZERO:
            totals[key] += tokens[key]
        model_cost = cost_usd(model, tokens)
        models.append({"model": model, **tokens, "cost_usd": model_cost})
        if model_cost is None:
            cost_known = False
        else:
            total_cost += model_cost
    return {
        "models": models,
        "totals": totals,
        "cost_usd": round(total_cost, 4) if models and cost_known else
                    (round(total_cost, 4) if total_cost else None),
    }


def tokens_for_work_item(conn, work_item_id: str) -> dict:
    """The ticket's sessions, summed whole (see the module docstring)."""
    session_ids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT s.session_id FROM activities a"
        " JOIN sessions s ON s.id = a.session_row_id"
        " WHERE a.work_item_id=? AND a.deleted_at IS NULL", (work_item_id,))]
    return tokens_for_sessions(conn, session_ids)


def format_tokens(count: int) -> str:
    """`1_234_567 -> "1.2M"`, `85_300 -> "85k"`, `950 -> "950"`."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)
