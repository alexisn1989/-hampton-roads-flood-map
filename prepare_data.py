"""Build the static data files for the Hampton Roads flood map.

Outputs (all written to data/):
  congressional_districts.geojson  VA congressional districts touching Hampton Roads
                                   (from the SCV FINAL CD shapefile in the VoteIQ repo)
  senate_districts.geojson         2024 state Senate districts in the region (Census TIGERweb)
  house_districts.geojson          2024 House of Delegates districts in the region (Census TIGERweb)
  localities.geojson               Hampton Roads city/county boundaries (Census TIGERweb)
  stations.json                    NOAA tide gauges with flood thresholds converted to ft MLLW
  civic_flood_watch.json           Norfolk/VB city council flood votes + donor-vote alignment
                                   (read-only export from the VoteIQ repo's polls.db — this
                                   script never writes to that database, and the running
                                   flood-map server never opens it either; this file is the
                                   only bridge between the two repos)

Run:  python prepare_data.py
"""

import gzip
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

BASE = Path(__file__).parent
DATA = BASE / "data"
VOTEIQ_REPO = Path(r"C:\Users\Alexis\OneDrive\Desktop\Vriginia_api_election")

CD_SHAPEFILE = VOTEIQ_REPO / "SCV Final 2021 Redistricting Plans" / "SCV FINAL CD.shp"
POLLS_DB = VOTEIQ_REPO / "polls.db"

# lon/lat bounding box covering the Hampton Roads planning district
BBOX = (-77.6, 36.50, -75.55, 37.70)
# ~20 m simplification; keeps GeoJSON small without visible distortion at map scale
SIMPLIFY_DEG = 0.0002

TIGER_LEGISLATIVE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer"
TIGER_COUNTY = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer"
MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations"

# The 16 Hampton Roads (HRPDC) localities, as state+county FIPS GEOIDs
LOCALITY_GEOIDS = [
    "51550",  # Chesapeake
    "51620",  # Franklin (city)
    "51073",  # Gloucester
    "51650",  # Hampton
    "51093",  # Isle of Wight
    "51095",  # James City
    "51700",  # Newport News
    "51710",  # Norfolk
    "51735",  # Poquoson
    "51740",  # Portsmouth
    "51175",  # Southampton
    "51800",  # Suffolk
    "51181",  # Surry
    "51810",  # Virginia Beach
    "51830",  # Williamsburg
    "51199",  # York
]

# NOAA CO-OPS water level stations in/around Hampton Roads
STATION_IDS = [
    "8638610",  # Sewells Point (Norfolk)
    "8639348",  # Money Point (Chesapeake)
    "8638863",  # Chesapeake Bay Bridge Tunnel
    "8637689",  # Yorktown USCG Training Center
    "8632200",  # Kiptopeke
]


def fetch_json(url: str, params: dict | None = None, tries: int = 3) -> dict:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw)
        except Exception as err:  # noqa: BLE001 - retry any transient failure
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def save_geojson(gdf: gpd.GeoDataFrame, name: str) -> None:
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    out = DATA / name
    gdf.to_file(out, driver="GeoJSON")
    print(f"  wrote {out.name}: {len(gdf)} features, {out.stat().st_size / 1024:.0f} KB")


def query_tiger_layer(service: str, layer_id: int, where: str) -> gpd.GeoDataFrame:
    params = {
        "where": where,
        "geometry": ",".join(str(v) for v in BBOX),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "BASENAME,NAME,GEOID",
        "outSR": "4326",
        "f": "geojson",
    }
    data = fetch_json(f"{service}/{layer_id}/query", params)
    feats = data.get("features", [])
    if not feats:
        raise RuntimeError(f"no features from {service} layer {layer_id}")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


def find_layer_id(service: str, layer_name: str) -> int:
    meta = fetch_json(service, {"f": "json"})
    for layer in meta["layers"]:
        if layer["name"] == layer_name and layer.get("type") == "Feature Layer":
            return layer["id"]
    raise RuntimeError(f"layer {layer_name!r} not found in {service}")


def build_congressional_districts() -> None:
    print("congressional districts (SCV FINAL CD shapefile)...")
    gdf = gpd.read_file(CD_SHAPEFILE)
    gdf = gdf.to_crs("EPSG:4326")
    region = box(*BBOX)
    gdf = gdf[gdf.geometry.intersects(region)][["DISTRICT", "geometry"]]
    save_geojson(gdf, "congressional_districts.geojson")


def build_legislative_districts() -> None:
    print("state legislative districts (TIGERweb 2024)...")
    sldu = query_tiger_layer(TIGER_LEGISLATIVE, 1, "STATE='51'")
    save_geojson(sldu, "senate_districts.geojson")
    sldl = query_tiger_layer(TIGER_LEGISLATIVE, 2, "STATE='51'")
    save_geojson(sldl, "house_districts.geojson")


def build_localities() -> None:
    print("localities (TIGERweb counties)...")
    layer_id = find_layer_id(TIGER_COUNTY, "Counties")
    geoid_list = ",".join(f"'{g}'" for g in LOCALITY_GEOIDS)
    gdf = query_tiger_layer(TIGER_COUNTY, layer_id, f"GEOID IN ({geoid_list})")
    save_geojson(gdf, "localities.geojson")


def build_stations() -> None:
    print("NOAA tide stations...")
    stations = []
    for sid in STATION_IDS:
        meta = fetch_json(f"{MDAPI}/{sid}.json")["stations"][0]
        entry = {
            "id": sid,
            "name": meta["name"],
            "lat": meta["lat"],
            "lon": meta["lng"],
            "thresholds_mllw": None,
        }
        # Flood levels come back relative to the station datum (STND).
        # Convert to MLLW so they compare directly with live water_level
        # observations requested with datum=MLLW.
        try:
            levels = fetch_json(f"{MDAPI}/{sid}/floodlevels.json")
            datums = fetch_json(f"{MDAPI}/{sid}/datums.json", {"units": "english"})
            mllw = next(d["value"] for d in datums["datums"] if d["name"] == "MLLW")
            thresholds = {}
            for key in ("action", "minor", "moderate", "major"):
                value = levels.get(f"nws_{key}", levels.get(f"nos_{key}", levels.get(key)))
                if value is not None:
                    thresholds[key] = round(value - mllw, 2)
            entry["thresholds_mllw"] = thresholds or None
        except Exception as err:  # noqa: BLE001 - station simply shown without thresholds
            print(f"  {sid}: no flood thresholds ({err})")
        stations.append(entry)
        print(f"  {sid} {entry['name']}: thresholds {entry['thresholds_mllw']}")
    out = DATA / "stations.json"
    out.write_text(json.dumps(stations, indent=2))
    print(f"  wrote {out.name}")


# Keyword filter for flood/stormwater-relevant council agenda items. Matched
# against free-text ordinance/resolution titles, so it's necessarily a blunt
# instrument — a false positive here just means an irrelevant item shows up,
# never a wrong number or fabricated vote (the underlying query still returns
# the real recorded title/date/result).
FLOOD_KEYWORDS = (
    "flood", "storm water", "stormwater", "drainage", "coastal storm",
    "resilien", "seawall", "sea wall", "bulkhead", "wetland", "shoreline",
    "levee", "dune", "erosion", "sea level",
)

# (locality name matching localities.geojson NAME, per-city table names)
CIVIC_CITIES = [
    ("Norfolk city", {
        "actions_table": "norfolk_council_votes",
        "actions_id": "agenda_item",
        "adjacency_table": "norfolk_donor_vote_adjacency",
    }),
    ("Virginia Beach city", {
        "actions_table": "vb_council_member_votes",  # no distinct per-action table; dedupe below
        "actions_id": "resolution_id",
        "adjacency_table": "vb_donor_vote_adjacency",
    }),
]


def _flood_keyword_where(column: str) -> str:
    clauses = " OR ".join(f"{column} LIKE '%{kw}%'" for kw in FLOOD_KEYWORDS)
    return f"({clauses})"


def build_civic_flood_watch() -> None:
    """Export Norfolk/VB city council flood-relevant votes + donor-vote
    alignment from the VoteIQ repo's polls.db. Read-only connection — this
    script only ever SELECTs, never writes to that database. Output is a
    static JSON file committed to this repo; the deployed flood-map server
    never opens polls.db itself, so there is no runtime coupling between
    the two services or their databases."""
    print("civic flood watch (Norfolk/VB council data from VoteIQ's polls.db)...")
    if not POLLS_DB.exists():
        print(f"  skipped: {POLLS_DB} not found (VoteIQ repo not present on this machine)")
        return

    con = sqlite3.connect(f"file:{POLLS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    result = {}
    try:
        for locality_name, cfg in CIVIC_CITIES:
            cur = con.cursor()

            # Distinct recent flood/stormwater-relevant actions.
            where = _flood_keyword_where("title")
            cur.execute(
                f"SELECT DISTINCT {cfg['actions_id']} AS action_id, title, result, "
                f"meeting_date FROM {cfg['actions_table']} WHERE {where} "
                f"AND meeting_date IS NOT NULL ORDER BY meeting_date DESC LIMIT 8"
            )
            actions = [
                {"title": r["title"], "result": r["result"], "date": r["meeting_date"]}
                for r in cur.fetchall()
            ]

            # Per-member donor-vote alignment specifically on infrastructure
            # spending votes — the closest available topic to flood/stormwater
            # capital projects (the underlying data has no flood-specific
            # topic tag). This is adjacency, not causation: it states what
            # fraction of a member's YES votes on infrastructure items lines
            # up with how much of their campaign funding came from a given
            # donor sector. Said explicitly in signal_text below, matching
            # the disclaimer already used on VoteIQ's own donor-alignment
            # tables elsewhere.
            cur.execute(
                f"SELECT member_name, sector, sector_pct, sector_amt, "
                f"member_yes_pct, council_yes_pct, delta_pp, topic_vote_count "
                f"FROM {cfg['adjacency_table']} WHERE topic='infrastructure' "
                f"ORDER BY member_name"
            )
            members = []
            for r in cur.fetchall():
                sign = "+" if r["delta_pp"] >= 0 else ""
                signal_text = (
                    f"{r['member_name']} ({r['sector']} donors, {r['sector_pct']:.0f}% of funds): "
                    f"votes YES on infrastructure items {r['member_yes_pct']:.0f}% of the time "
                    f"vs {r['council_yes_pct']:.0f}% council average ({sign}{r['delta_pp']:.0f}pp, "
                    f"{r['topic_vote_count']} votes). Adjacency only — not causal inference."
                )
                members.append({
                    "member": r["member_name"],
                    "donor_sector": r["sector"],
                    "sector_pct_of_funds": r["sector_pct"],
                    "sector_amount": r["sector_amt"],
                    "signal_text": signal_text,
                })

            result[locality_name] = {"recent_flood_actions": actions, "member_alignment": members}
            print(f"  {locality_name}: {len(actions)} flood-related actions, "
                  f"{len(members)} members with infrastructure alignment data")
    finally:
        con.close()

    out = DATA / "civic_flood_watch.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"  wrote {out.name}")


def main() -> None:
    DATA.mkdir(exist_ok=True)
    build_congressional_districts()
    build_legislative_districts()
    build_localities()
    build_stations()
    build_civic_flood_watch()
    print("done")


if __name__ == "__main__":
    main()
