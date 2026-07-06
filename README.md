# Hampton Roads Flood Map

Interactive web map of flood risk across the Hampton Roads region of Virginia,
layered with the political districts that represent the exposed areas.

## Layers

| Layer | Source | Notes |
|---|---|---|
| Sea level rise (1–10 ft) | NOAA Sea Level Rise Viewer tile services | Inundation above today's MHHW (high tide line). Scenario mapping, not a forecast. |
| FEMA flood zones | FEMA National Flood Hazard Layer (WMS) | Only draws when zoomed in (roughly zoom 13+) — a FEMA service scale limit. |
| Congressional districts | 2021 SCV Final CD shapefile (Virginia Supreme Court special masters plan) | |
| State Senate / House of Delegates districts | Census TIGERweb, 2024 vintage | |
| City & county boundaries | Census TIGERweb | The 16 HRPDC localities |
| Live tide gauges | NOAA CO-OPS API | Sewells Point, Money Point, Chesapeake Bay Bridge Tunnel, Yorktown, Kiptopeke. Auto-refreshes every 6 minutes. |

Gauge markers are colored by the latest observation against NWS flood
thresholds (converted to feet above MLLW so they compare directly with the
live reading).

## Property lookup

Type an address (or right-click anywhere, or drag the pin) to get a
point-specific report: FEMA flood zone, the lowest NOAA sea-level-rise
scenario that inundates the spot, the districts that represent it, and the
nearest tide gauge. Geocoding is OpenStreetMap Nominatim (the Census
geocoder doesn't send CORS headers); zone and SLR values are queried live
from FEMA NFHL and NOAA at the clicked point. Screening info only — not a
flood insurance determination.

## AI property report

Each lookup popup has a **Generate AI report** button: the collected data
(zone, SLR result, elevation vs BFE, claims history, districts, nearest
gauge) is sent to `POST /api/report`, where Claude writes a plain-English
homeowner report. The prompt is strictly grounded — Claude may only use the
queried values, must say when a field is unavailable, and never invents
numbers. Reports are cached in memory by payload hash. Model defaults to
`claude-haiku-4-5` (fractions of a cent per report); override with the
`FLOOD_MODEL` env var.

## Analytics

Three events — `pageview`, `lookup`, `ai_report` — are logged to a local
SQLite file (`analytics.db`, gitignored) via `POST /api/track`. No cookies,
no IP addresses, no per-visitor identifiers of any kind; just aggregate
counts. Rate-limited per IP (`TRACK_IP_LIMIT`, default 60/10min) separately
from the report endpoint's budget.

Counts are visible at `GET /api/stats?key=...`, which is a 404 (hidden
entirely) unless the `STATS_KEY` env var is set. Set it in Render, then
visit `https://www.floodwatchiq.com/api/stats?key=<that value>` to see
`all_time` / `last_24h` / `last_7d` counts per event.

## Feedback

Every lookup popup has a **Report an issue with this data** link. It
opens an inline form (no page navigation), and submits to `POST
/api/feedback` along with the full `lastLookup` context — zone, SLR
result, elevation, claims, districts, tide forecast, civic data — so a
report is actionable ("wrong zone at this address") instead of vague.
Same SQLite file as analytics, its own `feedback` table, rate-limited
separately (`FEEDBACK_IP_LIMIT`, default 5/10min — tighter than
tracking, since this is free text from the public).

Submitted feedback is visible at `GET /api/feedback?key=...`, gated by
the same `STATS_KEY` as `/api/stats`.

## Run it

```
pip install -r requirements.txt      # runtime deps only — no geopandas
python server.py                     # serves the map + AI report API on :8000
```

Then open http://localhost:8000

The server needs `ANTHROPIC_API_KEY` (from the environment, a `.env` here,
or the sibling VoteIQ repo's `.env` as a fallback). Without a key the map
still works fully — only the AI report button degrades with an explanatory
message. `python -m http.server` also still works for a no-backend static
preview.

`data/` is committed, so the map works out of the box without ever running
`prepare_data.py`. If a source changes and you need to rebuild it:

```
pip install -r requirements-dev.txt  # adds geopandas (heavy — dev only)
python prepare_data.py               # rebuilds data/*.geojson + stations.json
```

It needs `geopandas` (with `shapely`/`pyproj`) and network access to Census
TIGERweb and the NOAA metadata API. `geopandas` is deliberately excluded
from `requirements.txt` — it's not imported by `server.py`, and including it
would slow every deploy build for a dependency the running app never uses.

The congressional district source shapefile is read from the VoteIQ repo at
`..\Vriginia_api_election\SCV Final 2021 Redistricting Plans\` — adjust
`CD_SHAPEFILE` in `prepare_data.py` if that moves.

## Caveats

- SLR layers show *depth of inundation under a given water level scenario*,
  not the probability or timing of that scenario.
- Flood thresholds come from NOAA's station metadata (NWS values, station
  datum converted to MLLW). Stations without published thresholds show the
  live reading only.
- District boundaries are simplified ~20 m for file size; don't use them for
  parcel-level determinations.
