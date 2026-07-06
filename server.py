"""Flood map backend: serves the static map plus a Claude-powered report endpoint.

POST /api/report takes the property lookup JSON the frontend already collects
(FEMA zone, sea level rise result, elevation, NFIP claims, districts, nearest
gauge) and returns a plain-English homeowner report grounded ONLY in that data.

Run:  python server.py [--port 8000]
Needs ANTHROPIC_API_KEY (env, ./.env, or the VoteIQ repo's .env as fallback).
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
MODEL = os.environ.get("FLOOD_MODEL", "claude-haiku-4-5")

# Per-IP throttle: guards the endpoint itself (cache hits included) against
# scripted hammering. Daily cap: guards actual spend, so it only counts real
# Claude calls (cache hits are free and don't count against it).
IP_LIMIT = int(os.environ.get("REPORT_IP_LIMIT", "6"))
IP_WINDOW_SECONDS = int(os.environ.get("REPORT_IP_WINDOW_SECONDS", "600"))
DAILY_LIMIT = int(os.environ.get("REPORT_DAILY_LIMIT", "200"))

# Analytics: no cookies, no IP addresses stored, no third-party script — just
# aggregate counts of a handful of named events, in a local SQLite file.
ANALYTICS_DB = BASE / "analytics.db"
ALLOWED_EVENTS = {"pageview", "lookup", "ai_report"}
TRACK_IP_LIMIT = int(os.environ.get("TRACK_IP_LIMIT", "60"))
TRACK_IP_WINDOW_SECONDS = int(os.environ.get("TRACK_IP_WINDOW_SECONDS", "600"))
STATS_KEY = os.environ.get("STATS_KEY")

SYSTEM_PROMPT = """\
You write plain-English flood risk reports for properties in Hampton Roads, Virginia.

Rules — these are absolute:
- Use ONLY the data provided in the request. Never invent numbers, zones, dates,
  claim counts, or place names.
- If a field is missing or null, say that data wasn't available for this spot.
  Do not guess or fill the gap.
- You may explain what standard terms mean (Zone AE, base flood elevation,
  the NFIP, MLLW, NOAA sea level rise scenarios) from general knowledge, but
  every fact about THIS property must come from the provided data.
- NOAA sea level rise results are scenario mapping (inundation IF water rises
  N feet above today's high tide line), not a prediction of when.
- active_nws_flood_alerts, when present, are CURRENT National Weather Service
  alerts in effect at this location right now. Lead with them — they are
  time-sensitive. An empty list means no flood alerts are active, which says
  nothing about long-term risk.
- nearest_tide_gauge.next_high_tide, when present, is a NEAR-TERM forecast
  (the next high tide, hours away, from NOAA's astronomical predictions) —
  distinct from both the current-moment alerts and the long-term SLR
  scenarios. deltaText already states the comparison to flood stage
  (e.g. "0.3 ft above minor flood stage") — restate it plainly, don't
  recompute or second-guess the number.
- city_council_flood_watch, when present, is CITYWIDE local-government data
  (Norfolk or Virginia Beach only — most localities won't have this field at
  all, which means no data was collected for that city, not that the city
  council has done nothing). It is NOT specific to whoever represents this
  exact address — say so if you mention it. recent_flood_actions are real
  passed/failed city ordinances and resolutions on flood, stormwater, or
  shoreline projects. member_alignment signal_text strings already state
  they show donor-vote adjacency, not causation — never strengthen that
  into a causal claim (e.g. never say a donor "bought" a vote).
- This is screening information, not a flood insurance determination or legal
  advice — say so once, briefly.
- Audience: a homeowner or buyer with no flood expertise. Short sentences,
  no jargon without a one-phrase explanation.

Format: markdown, four sections with ## headings, under 350 words total:
## What we found
## Flood insurance
## Looking ahead
## Bottom line
"""


def _ensure_api_key() -> None:
    """Resolve ANTHROPIC_API_KEY from env, ./.env, or the sibling VoteIQ .env."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    candidates = [BASE / ".env", BASE.parent / "Vriginia_api_election" / ".env"]
    for env_file in candidates:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
                return


app = FastAPI(title="Hampton Roads Flood Map")

_report_cache: dict[str, str] = {}
_CACHE_MAX = 500

_ip_lock = threading.Lock()
_ip_hits: dict[tuple[str, str], deque] = defaultdict(deque)

_daily_lock = threading.Lock()
_daily_count = 0
_daily_day: str | None = None


def _client_ip(request: Request) -> str:
    # Render sits behind a proxy — the real client IP is the first hop in
    # X-Forwarded-For, not request.client.host (that would be the proxy).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_ip_rate_limit(
    ip: str, bucket: str, limit: int, window_seconds: int, message: str
) -> None:
    """Shared sliding-window limiter, keyed by (ip, bucket) so /api/report's
    Claude-cost budget and /api/track's much cheaper budget never share
    state or configuration."""
    now = time.time()
    with _ip_lock:
        hits = _ip_hits[(ip, bucket)]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
            raise HTTPException(
                429, f"{message} Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


def _init_analytics_db() -> None:
    con = sqlite3.connect(ANALYTICS_DB)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts TEXT DEFAULT CURRENT_TIMESTAMP,"
            " event TEXT NOT NULL,"
            " path TEXT,"
            " referrer TEXT"
            ")"
        )
        con.commit()
    finally:
        con.close()


_init_analytics_db()


def _record_event(event: str, path: str | None, referrer: str | None) -> None:
    # A short-lived connection per call sidesteps sqlite3's not-thread-safe-
    # across-threads default — FastAPI's sync routes run in a thread pool,
    # and this write is cheap enough that connection setup cost doesn't matter.
    con = sqlite3.connect(ANALYTICS_DB)
    try:
        con.execute(
            "INSERT INTO events (event, path, referrer) VALUES (?, ?, ?)",
            (event, (path or "")[:300], (referrer or "")[:300]),
        )
        con.commit()
    finally:
        con.close()


def _check_daily_cap() -> None:
    global _daily_count, _daily_day
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _daily_lock:
        if _daily_day != today:
            _daily_day = today
            _daily_count = 0
        if _daily_count >= DAILY_LIMIT:
            raise HTTPException(429, "Daily report limit reached — please try again tomorrow.")
        _daily_count += 1


class ReportRequest(BaseModel):
    lookup: dict


class TrackRequest(BaseModel):
    event: str
    path: str | None = None
    referrer: str | None = None


@app.post("/api/track", status_code=204)
def track_event(req: TrackRequest, request: Request) -> None:
    if req.event not in ALLOWED_EVENTS:
        raise HTTPException(400, f"unknown event {req.event!r}")
    _check_ip_rate_limit(
        _client_ip(request), "track", TRACK_IP_LIMIT, TRACK_IP_WINDOW_SECONDS,
        "Too many events from this address.",
    )
    _record_event(req.event, req.path, req.referrer)


@app.get("/api/stats")
def stats(key: str | None = None) -> dict:
    """Aggregate event counts only — no per-visitor data exists to leak.
    Hidden entirely (404) unless STATS_KEY is set in the environment."""
    if not STATS_KEY:
        raise HTTPException(404)
    if key != STATS_KEY:
        raise HTTPException(403, "bad key")

    con = sqlite3.connect(ANALYTICS_DB)
    try:
        def counts(where: str = "1=1") -> dict:
            rows = con.execute(
                f"SELECT event, COUNT(*) FROM events WHERE {where} GROUP BY event"
            ).fetchall()
            return {event: n for event, n in rows}

        return {
            "all_time": counts(),
            "last_24h": counts("ts >= datetime('now', '-1 day')"),
            "last_7d": counts("ts >= datetime('now', '-7 day')"),
        }
    finally:
        con.close()


@app.get("/api/health")
def health() -> dict:
    """Deployment diagnostics — booleans only, never secret values."""
    _ensure_api_key()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    # Surface near-miss env var names (e.g. a typo like "Anthorpic") without
    # exposing anything: report names only, for vars whose name hints at intent.
    suspects = [
        name for name in os.environ
        if name != "ANTHROPIC_API_KEY"
        and ("ANTHROP" in name.upper() or "CLAUDE" in name.upper())
    ]
    return {
        "status": "ok",
        "model": MODEL,
        "api_key_configured": bool(key.strip()),
        "similar_env_var_names": suspects,
        "reports_today": _daily_count,
        "daily_limit": DAILY_LIMIT,
    }


@app.post("/api/report")
def generate_report(req: ReportRequest, request: Request) -> dict:
    _check_ip_rate_limit(
        _client_ip(request), "report", IP_LIMIT, IP_WINDOW_SECONDS,
        "Too many report requests from this address.",
    )

    _ensure_api_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server")

    cache_key = hashlib.sha256(
        json.dumps(req.lookup, sort_keys=True).encode()
    ).hexdigest()
    if cache_key in _report_cache:
        return {"report": _report_cache[cache_key], "cached": True, "model": MODEL}

    _check_daily_cap()

    client = anthropic.Anthropic()
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": "Write the flood risk report for this lookup result:\n"
                           f"```json\n{json.dumps(req.lookup, indent=2)}\n```",
            }],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Anthropic API key was rejected")
    except anthropic.APIStatusError as err:
        raise HTTPException(502, f"Claude API error: {err.message}")

    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise HTTPException(502, "Claude returned an empty report")

    if len(_report_cache) >= _CACHE_MAX:
        _report_cache.clear()
    _report_cache[cache_key] = text
    return {"report": text, "cached": False, "model": MODEL}


# Static site (index.html + data/) — mounted last so /api/* wins
app.mount("/", StaticFiles(directory=str(BASE), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # Render (and most PaaS hosts) inject PORT and expect a bind on 0.0.0.0.
    port = int(os.environ.get("PORT", 8000))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    uvicorn.run(app, host="0.0.0.0", port=port)
