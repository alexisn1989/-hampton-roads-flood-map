"""Build the static data files for the Hampton Roads flood map.

Outputs (all written to data/):
  congressional_districts.geojson  VA congressional districts touching Hampton Roads
                                   (from the SCV FINAL CD shapefile in the VoteIQ repo)
  senate_districts.geojson         2024 state Senate districts in the region (Census TIGERweb)
  house_districts.geojson          2024 House of Delegates districts in the region (Census TIGERweb)
  localities.geojson               Hampton Roads city/county boundaries (Census TIGERweb)
  stations.json                    NOAA tide gauges with flood thresholds converted to ft MLLW

Run:  python prepare_data.py
"""

import gzip
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

BASE = Path(__file__).parent
DATA = BASE / "data"

CD_SHAPEFILE = Path(
    r"C:\Users\Alexis\OneDrive\Desktop\Vriginia_api_election"
    r"\SCV Final 2021 Redistricting Plans\SCV FINAL CD.shp"
)

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


def main() -> None:
    DATA.mkdir(exist_ok=True)
    build_congressional_districts()
    build_legislative_districts()
    build_localities()
    build_stations()
    print("done")


if __name__ == "__main__":
    main()
