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
import sys
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
MODEL = os.environ.get("FLOOD_MODEL", "claude-haiku-4-5")

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


class ReportRequest(BaseModel):
    lookup: dict


@app.post("/api/report")
def generate_report(req: ReportRequest) -> dict:
    _ensure_api_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server")

    cache_key = hashlib.sha256(
        json.dumps(req.lookup, sort_keys=True).encode()
    ).hexdigest()
    if cache_key in _report_cache:
        return {"report": _report_cache[cache_key], "cached": True, "model": MODEL}

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

    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    uvicorn.run(app, host="127.0.0.1", port=port)
