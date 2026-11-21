"""Authentication + per-team daily quotas."""

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import yaml


class QuotaExceeded(Exception):
    def __init__(self, team: str, limit: int):
        super().__init__(f"team '{team}' exceeded its daily quota of {limit} calls")
        self.team, self.limit = team, limit


@dataclass(frozen=True)
class TeamAccount:
    team: str
    api_key_hash: str
    daily_quota: int = 5000
    priority: int = 2
    monthly_budget_usd: float = 0.0

    @staticmethod
    def hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @classmethod
    def create(cls, team: str, api_key: str, **kw) -> "TeamAccount":
        return cls(team=team, api_key_hash=cls.hash_key(api_key), **kw)


PLACEHOLDER_KEY_PREFIXES = ("change-me", "your-", "sk-ant-your")


def load_teams(path: str, env: Optional[dict] = None) -> list[TeamAccount]:
    """Load teams.yaml; API keys come from env vars. Teams without a key set are skipped.
    Placeholder keys copied verbatim from .env.example are refused (they are public)."""
    env = os.environ if env is None else env
    with open(path) as fh:
        data = yaml.safe_load(fh)
    accounts = []
    for t in data["teams"]:
        key = env.get(t["api_key_env"])
        if not key:
            continue
        if key.lower().startswith(PLACEHOLDER_KEY_PREFIXES):
            raise ValueError(f"{t['api_key_env']} still holds the .env.example placeholder; "
                             "set a real secret before starting the gateway")
        accounts.append(TeamAccount.create(
            t["team"], key, daily_quota=t.get("daily_quota", 5000),
            priority=t.get("priority", 2), monthly_budget_usd=t.get("monthly_budget_usd", 0.0)))
    return accounts


class AuthRegistry:
    def __init__(self, accounts: list[TeamAccount]):
        self._by_hash = {a.api_key_hash: a for a in accounts}
        self._by_team = {a.team: a for a in accounts}

    def authenticate(self, api_key: Optional[str]) -> Optional[TeamAccount]:
        if not api_key:
            return None
        return self._by_hash.get(TeamAccount.hash_key(api_key))

    def team(self, name: str) -> Optional[TeamAccount]:
        return self._by_team.get(name)

    def teams(self) -> list[TeamAccount]:
        return list(self._by_team.values())


class QuotaManager:
    """Counts calls per team per UTC day. Check-and-increment is atomic within one process
    (the gateway runs sync handlers on a thread pool); it is NOT shared across processes."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def consume(self, account: TeamAccount) -> int:
        with self._lock:
            key = (account.team, self._day())
            used = self._counts.get(key, 0)
            if used >= account.daily_quota:
                raise QuotaExceeded(account.team, account.daily_quota)
            self._counts[key] = used + 1
            return account.daily_quota - used - 1

    def used(self, team: str) -> int:
        return self._counts.get((team, self._day()), 0)
