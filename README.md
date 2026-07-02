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

## Run it

```
python prepare_data.py      # one-time: builds data/*.geojson + stations.json
python -m http.server 8000
```

Then open http://localhost:8000

`prepare_data.py` needs `geopandas` (with `shapely`/`pyproj`) and network
access to Census TIGERweb and the NOAA metadata API. The generated files in
`data/` are committed, so the map works without re-running it.

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
