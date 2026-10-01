"""Read this machine's Cursor login and the current period's two usage pools.

The access token stays in memory. This module never writes Cursor's state
database and never logs the token.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
REFRESH_URL = "https://api2.cursor.sh/oauth/token"
# Cursor desktop's public OAuth client id. Not a user secret.
OAUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"

_TRANSIENT = "塔菲看走眼了，待会再看"
_NO_LOGIN = "没找到 Cursor 登录"
_EXPIRED = "登录过期了，重新打开一下 Cursor"
_NO_POOLS = "接口没带回额度"


@dataclass
class QuotaSnapshot:
    """One reading of the current billing period.

    Cents fields are integers, matching GetCurrentPeriodUsage. Percents are
    JSON numbers: Auto has been seen as float, API as int. Missing keys stay
    None — callers must not invent a percent or a dollar amount.
    """

    auto_percent: float | None
    api_percent: float | None
    included_spend_cents: int | None
    limit_cents: int | None
    remaining_cents: int | None
    missing_note: str | None = None


@dataclass
class FetchResult:
    """Outcome of one usage request.

    keep_previous: the network or the database blipped; the UI should keep
    the last snapshot and show message. When keep_previous is false and
    snapshot is None, message is a login problem and the old numbers go away.
    """

    snapshot: QuotaSnapshot | None
    message: str | None = None
    keep_previous: bool = False


def plan_used_percent(snapshot: QuotaSnapshot) -> float | None:
    """Included-plan dollars already spent, as a percent.

    Uses includedSpend / limit. Returns None when limit is missing or not a
    positive int, so a zero limit is not turned into a fake ratio.
    """

    spent = snapshot.included_spend_cents
    limit = snapshot.limit_cents
    if not isinstance(spent, int) or not isinstance(limit, int) or limit <= 0:
        return None
    return spent / limit * 100.0


def alert_percent(snapshot: QuotaSnapshot) -> float | None:
    """Highest of plan spend, Auto, and API. None when none of them exist."""

    values: list[float] = []
    plan = plan_used_percent(snapshot)
    if plan is not None:
        values.append(plan)
    if snapshot.auto_percent is not None:
        values.append(snapshot.auto_percent)
    if snapshot.api_percent is not None:
        values.append(snapshot.api_percent)
    if not values:
        return None
    return max(values)


def mood_level(alert: float | None) -> int:
    """Map occupancy to a reaction: 0 calm, 1 uneasy, 2 alarm, 3 panic."""

    if alert is None:
        return 0
    if alert >= 95.0:
        return 3
    if alert >= 80.0:
        return 2
    if alert >= 50.0:
        return 1
    return 0


def alert_tier(alert: float | None) -> int:
    """0 under 80, 1 from 80, 2 from 100. A tier only matters when it rises."""

    if alert is None or alert < 80.0:
        return 0
    if alert >= 100.0:
        return 2
    return 1


def parse_period_usage(payload: dict) -> QuotaSnapshot:
    """Pull Auto, API, and included cents out of a GetCurrentPeriodUsage body.

    Accepts only the types observed on the wire. A bool is not treated as a
    number. A float cent value is ignored rather than rounded into an int.
    """

    plan = payload.get("planUsage")
    if not isinstance(plan, dict):
        return QuotaSnapshot(None, None, None, None, None, _NO_POOLS)

    auto = _percent(plan.get("autoPercentUsed")) if "autoPercentUsed" in plan else None
    api = _percent(plan.get("apiPercentUsed")) if "apiPercentUsed" in plan else None
    spent = _cents(plan.get("includedSpend")) if "includedSpend" in plan else None
    limit = _cents(plan.get("limit")) if "limit" in plan else None
    remaining = _cents(plan.get("remaining")) if "remaining" in plan else None

    missing: list[str] = []
    if auto is None:
        missing.append("Auto")
    if api is None:
        missing.append("API")
    note = "、".join(missing) + " 分项没返回" if missing else None
    return QuotaSnapshot(auto, api, spent, limit, remaining, note)


class CursorUsageClient:
    """Fetch current-period usage with the local Cursor session."""

    def __init__(self) -> None:
        self._memory_token: str | None = None

    def fetch(self) -> FetchResult:
        """Return a snapshot, a login error, or a transient miss.

        Transient failures set keep_previous so the bubble can keep the last
        good numbers. Tokens are never included in the result.
        """

        try:
            return self._fetch()
        except Exception:
            return FetchResult(None, _TRANSIENT, keep_previous=True)

    def _fetch(self) -> FetchResult:
        try:
            token = self._select_token()
        except sqlite3.Error:
            return FetchResult(None, _TRANSIENT, keep_previous=True)
        if token is None:
            return FetchResult(None, _NO_LOGIN, keep_previous=False)

        status, payload = _post_json(
            USAGE_URL,
            {},
            {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
        )
        if status == 401:
            try:
                refreshed = self._refresh_memory_token()
            except sqlite3.Error:
                return FetchResult(None, _TRANSIENT, keep_previous=True)
            if not refreshed or not self._memory_token:
                return FetchResult(None, _EXPIRED, keep_previous=False)
            status, payload = _post_json(
                USAGE_URL,
                {},
                {
                    "Authorization": "Bearer " + self._memory_token,
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                },
            )
        if status == 401:
            return FetchResult(None, _EXPIRED, keep_previous=False)
        if status != 200 or not isinstance(payload, dict):
            return FetchResult(None, _TRANSIENT, keep_previous=True)
        if "planUsage" not in payload and isinstance(payload.get("code"), str):
            if payload["code"] in {"unauthenticated", "permission_denied"}:
                return FetchResult(None, _EXPIRED, keep_previous=False)
            return FetchResult(None, _TRANSIENT, keep_previous=True)
        return FetchResult(parse_period_usage(payload))

    def _select_token(self) -> str | None:
        """Prefer a non-expiring DB token, then an in-memory refresh."""

        db_token = _read_item("cursorAuth/accessToken")
        if db_token and not _expiring(db_token):
            return db_token
        if self._memory_token and not _expiring(self._memory_token):
            return self._memory_token
        if db_token or _read_item("cursorAuth/refreshToken"):
            if self._refresh_memory_token() and self._memory_token:
                return self._memory_token
        return db_token or self._memory_token

    def _refresh_memory_token(self) -> bool:
        """Exchange the local refresh token. The new access token stays in memory."""

        refresh = _read_item("cursorAuth/refreshToken")
        if not refresh:
            return False
        status, payload = _post_json(
            REFRESH_URL,
            {
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": refresh,
            },
            {"Content-Type": "application/json"},
        )
        if status != 200 or not isinstance(payload, dict):
            return False
        if payload.get("shouldLogout") is True:
            self._memory_token = None
            return False
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            return False
        self._memory_token = token
        return True


def _state_db_path() -> Path | None:
    root = os.environ.get("APPDATA")
    if not root:
        return None
    return Path(root) / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _read_item(key: str) -> str | None:
    """Read one ItemTable value. Opens the database read-only and closes it."""

    path = _state_db_path()
    if path is None or not path.is_file():
        return None
    uri = "file:" + path.as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1.5)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    finally:
        con.close()
    if not row or not isinstance(row[0], str) or not row[0]:
        return None
    return row[0]


def _jwt_exp(token: str) -> int | None:
    """Read the exp claim without verifying the signature. None if absent."""

    try:
        payload_b64 = token.split(".")[1]
    except IndexError:
        return None
    pad = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        return None
    return exp


def _expiring(token: str, within: int = 60) -> bool:
    """True when exp is known and falls inside the next `within` seconds."""

    exp = _jwt_exp(token)
    if exp is None:
        return False
    return exp <= int(time.time()) + within


def _percent(value: object) -> float | None:
    """JSON number to float. Bools are rejected (they are ints in Python)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _cents(value: object) -> int | None:
    """Cents as an int. Floats are not rounded into cents."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _post_json(url: str, body: dict, headers: dict[str, str]) -> tuple[int, dict | list | None]:
    """POST JSON and return (status, parsed body). Body is None when it is not JSON."""

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(1_000_000)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    if isinstance(parsed, (dict, list)):
        return status, parsed
    return status, None


def _self_check() -> None:
    """Check the parser and mood bands against the observed payload shape."""

    sample = {
        "planUsage": {
            "totalSpend": 730,
            "includedSpend": 730,
            "remaining": 1270,
            "limit": 2000,
            "remainingBonus": False,
            "autoPercentUsed": 1.6222222222222222,
            "apiPercentUsed": 0,
            "totalPercentUsed": 1.54,
        }
    }
    snap = parse_period_usage(sample)
    assert snap.auto_percent is not None and abs(snap.auto_percent - 1.6222222222222222) < 1e-9
    assert snap.api_percent == 0.0
    assert snap.included_spend_cents == 730
    assert snap.limit_cents == 2000
    assert snap.remaining_cents == 1270
    assert snap.missing_note is None
    assert abs((plan_used_percent(snap) or 0) - 36.5) < 1e-9
    assert mood_level(alert_percent(snap)) == 0
    assert alert_tier(alert_percent(snap)) == 0

    assert parse_period_usage({}).missing_note == _NO_POOLS
    bad = parse_period_usage(
        {"planUsage": {"autoPercentUsed": True, "includedSpend": 1.5, "limit": 2000}}
    )
    assert bad.auto_percent is None
    assert bad.included_spend_cents is None
    assert bad.limit_cents == 2000
    assert bad.missing_note is not None and "Auto" in bad.missing_note

    assert mood_level(49.9) == 0
    assert mood_level(50) == 1
    assert mood_level(79.9) == 1
    assert mood_level(80) == 2
    assert mood_level(94.9) == 2
    assert mood_level(95) == 3
    assert mood_level(100) == 3
    assert alert_tier(79.9) == 0
    assert alert_tier(80) == 1
    assert alert_tier(100) == 2
    panic = QuotaSnapshot(1.0, 100.0, 100, 2000, 1900, None)
    assert mood_level(alert_percent(panic)) == 3
    assert alert_tier(alert_percent(panic)) == 2


if __name__ == "__main__":
    _self_check()
    print("ok")
