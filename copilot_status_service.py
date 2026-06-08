#!/usr/bin/env python3
"""Background status writer for the unified GNOME usage indicator.

This script fetches GitHub Copilot usage from GitHub Billing APIs and writes a
compact cache consumed by the GNOME Shell extension.
"""

import argparse
import calendar
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / ".cache" / "copilot-usage"
STATUS_PATH = CACHE_DIR / "status.json"
AI_CREDITS_URL = "https://api.github.com/users/{}/settings/billing/usage"
LEGACY_PREMIUM_REQUEST_URL = (
    "https://api.github.com/users/{}/settings/billing/premium_request/usage"
)
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
}
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20
MAX_REQUEST_ATTEMPTS = 3
DEFAULT_AI_CREDITS_LIMIT = 1500
DEFAULT_LEGACY_REQUEST_LIMIT = 300
DEFAULT_INTERVAL_SECONDS = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def _load_env():
    if load_dotenv:
        load_dotenv(PROJECT_DIR / ".env")
        return
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _updated_text(now):
    return "Updated " + now.strftime("%H:%M:%S")


def _request_usage(url, headers, params):
    last_exception = None

    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            return requests.get(
                url,
                headers=headers,
                params=params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            last_exception = exc
            if attempt == MAX_REQUEST_ATTEMPTS:
                break
            time.sleep(attempt)

    message = f"Network error while contacting GitHub API: {last_exception}"
    logger.error(message)
    raise RuntimeError(message) from last_exception


def _quantity(item):
    for key in ("quantity", "grossQuantity", "netQuantity"):
        value = item.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _usage_day(item):
    date_str = str(item.get("date") or item.get("timestamp") or "")
    if not date_str:
        return None
    try:
        return int(date_str[:10].split("-")[2])
    except (IndexError, ValueError):
        return None


def _is_copilot_ai_credit_item(item):
    product = str(item.get("product") or "").lower()
    sku = str(item.get("sku") or "").lower()
    unit_type = str(item.get("unitType") or "").lower()

    if "copilot" not in product and "copilot" not in sku:
        return False
    return (
        "ai credit" in sku
        or sku == "copilot_ai_unit"
        or unit_type in {"aicredits", "ai-units", "ai credits"}
    )


def _is_legacy_premium_request_item(item):
    return (
        item.get("product") == "Copilot"
        and item.get("sku") == "Copilot Premium Request"
    )


def _parse_usage_items(data, predicate, default_model):
    total = 0.0
    usage_items = []
    daily_usage = {}

    for item in data.get("usageItems", []):
        if not predicate(item):
            continue
        qty = _quantity(item)
        total += qty
        usage_items.append((item.get("model") or item.get("sku") or default_model, qty))

        day = _usage_day(item)
        if day is not None:
            daily_usage[day] = daily_usage.get(day, 0.0) + qty

    return total, usage_items, daily_usage


def _response_error(response, username):
    if response.status_code == 401:
        return "Bad credentials. Check GITHUB_TOKEN and token expiry."
    if response.status_code == 403:
        return "Forbidden. Check your token permissions (needs Plan read access)."
    if response.status_code == 404:
        return f"User '{username}' not found or no billing data."
    return f"HTTP {response.status_code} - {response.text}"


def _get_usage_for_month(token, username, year, month):
    params = {"year": year, "month": month}
    headers = HEADERS.copy()
    headers["Authorization"] = f"Bearer {token}"

    response = _request_usage(AI_CREDITS_URL.format(username), headers, params)
    if response.status_code != 200:
        raise RuntimeError(_response_error(response, username))

    data = response.json()
    total, usage_items, daily_usage = _parse_usage_items(
        data, _is_copilot_ai_credit_item, "Copilot AI Credits"
    )
    metadata = {
        "billing_mode": "ai_credits",
        "unit": "credits",
        "unit_label": "AI credits",
        "source_endpoint": "billing_usage",
    }

    if usage_items or total > 0:
        return total, usage_items, daily_usage, metadata

    legacy_response = _request_usage(
        LEGACY_PREMIUM_REQUEST_URL.format(username), headers, params
    )
    if legacy_response.status_code == 200:
        legacy_total, legacy_items, legacy_daily = _parse_usage_items(
            legacy_response.json(),
            _is_legacy_premium_request_item,
            "Copilot Premium Request",
        )
        if legacy_items or legacy_total > 0:
            return legacy_total, legacy_items, legacy_daily, {
                "billing_mode": "premium_requests",
                "unit": "requests",
                "unit_label": "requests",
                "source_endpoint": "premium_request_usage",
            }

    return total, usage_items, daily_usage, metadata


def _calculate_usage_metrics(limit, usage_used, current_day, days_in_month):
    pct_used = (usage_used / limit) * 100 if limit > 0 else 0.0
    pct_remaining = max(0.0, 100.0 - pct_used) if limit > 0 else 0.0
    remaining = max(limit - usage_used, 0)
    remaining_pct = (remaining / limit) * 100 if limit > 0 else 0.0
    daily_allowance = limit / days_in_month if days_in_month > 0 else 0.0
    max_use_today = (limit * current_day) / days_in_month if days_in_month > 0 else 0.0
    daily_budget_delta = max_use_today - usage_used

    return {
        "pct_used": pct_used,
        "pct_remaining": pct_remaining,
        "remaining_requests": remaining,
        "remaining_requests_pct": remaining_pct,
        "daily_allowance": daily_allowance,
        "max_use_today": max_use_today,
        "daily_budget_delta": daily_budget_delta,
        "is_over_budget": usage_used > max_use_today,
    }


def _usage_limit(metadata):
    if metadata.get("billing_mode") == "premium_requests":
        env_limit = os.getenv("GITHUB_MONTHLY_LIMIT")
        default_limit = DEFAULT_LEGACY_REQUEST_LIMIT
    else:
        env_limit = os.getenv("GITHUB_AI_CREDITS_LIMIT")
        default_limit = DEFAULT_AI_CREDITS_LIMIT

    try:
        return float(env_limit) if env_limit else float(default_limit)
    except ValueError:
        return float(default_limit)


def _calculate_projection(limit, usage_used, current_day, days_in_month, now):
    if current_day <= 0 or usage_used <= 0:
        return "No usage data yet"
    if usage_used >= limit:
        return "Monthly limit reached"

    avg_daily = usage_used / current_day
    remaining_days = days_in_month - current_day
    projected_total = usage_used + (avg_daily * remaining_days)

    if projected_total <= limit:
        remaining_usage = max(limit - usage_used, 0)
        days_until_empty = remaining_usage / avg_daily if avg_daily > 0 else 999
        empty_date = now + timedelta(days=days_until_empty)
        return "On track. Run out ~" + empty_date.strftime("%b %d")

    days_until_over = (limit - usage_used) / avg_daily if avg_daily > 0 else 0
    over_date = now + timedelta(days=days_until_over)
    over_day = int(current_day + days_until_over)
    return "Will exceed on day {} ({})".format(over_day, over_date.strftime("%b %d"))


def _top_models(usage_items):
    items = sorted(usage_items or [], key=lambda item: item[1], reverse=True)
    return [
        {
            "model": str(model),
            "quantity": float(quantity),
        }
        for model, quantity in items[:8]
    ]


def _write_status(payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATUS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(STATUS_PATH)


def _read_status():
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _error_payload(message):
    now = datetime.now()
    return {
        "provider": "GitHub Copilot",
        "state": "error",
        "label": "Copilot --",
        "error": message,
        "updated": _updated_text(now),
        "timestamp": now.isoformat(),
    }


def build_status():
    _load_env()

    token = os.getenv("GITHUB_TOKEN")
    username = os.getenv("GITHUB_USERNAME")
    if not token or not username:
        raise RuntimeError("GITHUB_TOKEN or GITHUB_USERNAME not set")

    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    usage_used, usage_items, daily_usage, metadata = _get_usage_for_month(
        token, username, now.year, now.month
    )
    limit = _usage_limit(metadata)
    unit_label = metadata.get("unit_label", "usage")

    usage_used_today = daily_usage.get(now.day, 0)
    if usage_used_today == 0 and now_utc.day != now.day:
        usage_used_today = daily_usage.get(now_utc.day, 0)

    metrics = _calculate_usage_metrics(limit, usage_used, now.day, days_in_month)
    pct_remaining = metrics["remaining_requests_pct"]
    pct_used = metrics["pct_used"]

    payload = {
        "provider": "GitHub Copilot",
        "state": "ready",
        "label": "Copilot {}%".format(round(pct_remaining)),
        "username": username,
        "year": now.year,
        "month": now.month,
        "billing_mode": metadata.get("billing_mode"),
        "unit": metadata.get("unit"),
        "unit_label": unit_label,
        "source_endpoint": metadata.get("source_endpoint"),
        "limit": limit,
        "usage_used": usage_used,
        "usage_used_today": usage_used_today,
        "usage_remaining": metrics["remaining_requests"],
        "requests_used": usage_used,
        "requests_used_today": usage_used_today,
        "remaining_requests": metrics["remaining_requests"],
        "pct_used": pct_used,
        "pct_remaining": pct_remaining,
        "daily_allowance": metrics["daily_allowance"],
        "max_use_today": metrics["max_use_today"],
        "daily_budget_delta": metrics["daily_budget_delta"],
        "is_over_budget": metrics["is_over_budget"],
        "projection": _calculate_projection(limit, usage_used, now.day, days_in_month, now),
        "top_models": _top_models(usage_items),
        "updated": _updated_text(now),
        "timestamp": now.isoformat(),
    }
    if metadata.get("billing_mode") == "ai_credits":
        payload.update(
            {
                "credits_used": usage_used,
                "credits_used_today": usage_used_today,
                "credits_remaining": metrics["remaining_requests"],
                "credits_limit": limit,
            }
        )
    return payload


def refresh_once():
    try:
        payload = build_status()
    except Exception as exc:
        logger.error("Failed to refresh Copilot usage: %s", exc)
        payload = _read_status()
        if payload and payload.get("state") == "ready":
            now = datetime.now()
            payload["stale"] = True
            payload["refresh_error"] = str(exc)
            payload["last_refresh_attempt"] = _updated_text(now)
            payload["last_refresh_attempt_timestamp"] = now.isoformat()
        else:
            payload = _error_payload(str(exc))

    _write_status(payload)
    return payload


def run_loop(interval):
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopped:
        refresh_once()
        for _ in range(max(1, interval)):
            if stopped:
                break
            time.sleep(1)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="refresh once and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="refresh interval in seconds for service mode",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.once:
        refresh_once()
        return 0

    run_loop(max(30, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
