"""Rule-based scoring for the critical path (no external calls, deterministic).

Rules from the article's fraud rule engine:
  1. amount > $10,000
  2. login from a different country in the last 24h
  3. transaction velocity > 3 per hour
  4. high-risk merchant category + unusual amount
  5. multiple failed logins in the last hour + transaction
"""

from src.models import Event, RuleScore, risk_band
from src.state_store import align_ids

HIGH_RISK_CATEGORIES = {"crypto", "gambling", "gift_cards", "wire_transfer", "money_transfer"}

AMOUNT_THRESHOLD = 10_000.0
VELOCITY_THRESHOLD = 3          # per hour
FAILED_LOGIN_THRESHOLD = 3
UNUSUAL_AMOUNT_MULTIPLE = 3.0   # x typical amount

RULE_POINTS = {
    "high_amount": 40,
    "foreign_login": 30,
    "velocity": 25,
    "risky_merchant_unusual_amount": 25,
    "failed_logins_then_txn": 20,
}


def score_event(event: Event, ctx: dict) -> RuleScore:
    fired = []
    if event.amount > AMOUNT_THRESHOLD:
        fired.append("high_amount")

    countries = ctx.get("login_countries_24h") or []
    if any(c != event.country for c in countries):
        fired.append("foreign_login")

    hour_ago = event.event_ts - 3600
    ts = list(ctx.get("recent_txn_ts", []))
    ids = list(ctx.get("recent_txn_ids", []))
    ids = align_ids(ts, ids)
    # a replayed event is already in its own history: do not count it twice
    recent = [t for t, i in zip(ts, ids) if hour_ago <= t <= event.event_ts and i != event.event_id]
    if len(recent) + 1 > VELOCITY_THRESHOLD:  # include current event
        fired.append("velocity")

    typical = ctx.get("typical_amount") or 100.0
    if event.merchant_category in HIGH_RISK_CATEGORIES and event.amount > UNUSUAL_AMOUNT_MULTIPLE * typical:
        fired.append("risky_merchant_unusual_amount")

    if ctx.get("failed_logins_1h", 0) >= FAILED_LOGIN_THRESHOLD:
        fired.append("failed_logins_then_txn")

    score = min(100, sum(RULE_POINTS[r] for r in fired))
    return RuleScore(
        event_id=event.event_id,
        user_id=event.user_id,
        score=score,
        band=risk_band(score),
        rules_fired=fired,
    )
