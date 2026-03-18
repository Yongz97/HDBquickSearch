#!/usr/bin/env python3
"""Fetch HDB resale flat prices from data.gov.sg and output block-level aggregated GeoJSON."""

import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

DATASET_ID = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"
COORDS_CACHE = "block_coords.json"

MAPBOX_TOKEN = None

# Fallback town-center coords for blocks that fail geocoding
TOWN_COORDS = {
    "ANG MO KIO": [103.8490, 1.3691],
    "BEDOK": [103.9273, 1.3236],
    "BISHAN": [103.8352, 1.3526],
    "BUKIT BATOK": [103.7637, 1.3590],
    "BUKIT MERAH": [103.8239, 1.2819],
    "BUKIT PANJANG": [103.7719, 1.3774],
    "BUKIT TIMAH": [103.7764, 1.3294],
    "CENTRAL AREA": [103.8536, 1.2884],
    "CHOA CHU KANG": [103.7446, 1.3840],
    "CLEMENTI": [103.7649, 1.3150],
    "GEYLANG": [103.8842, 1.3201],
    "HOUGANG": [103.8863, 1.3612],
    "JURONG EAST": [103.7436, 1.3329],
    "JURONG WEST": [103.6946, 1.3404],
    "KALLANG/WHAMPOA": [103.8651, 1.3100],
    "MARINE PARADE": [103.9000, 1.3020],
    "PASIR RIS": [103.9494, 1.3721],
    "PUNGGOL": [103.9077, 1.3984],
    "QUEENSTOWN": [103.7981, 1.2942],
    "SEMBAWANG": [103.8185, 1.4491],
    "SENGKANG": [103.8914, 1.3868],
    "SERANGOON": [103.8670, 1.3554],
    "TAMPINES": [103.9568, 1.3496],
    "TOA PAYOH": [103.8563, 1.3343],
    "WOODLANDS": [103.7867, 1.4382],
    "YISHUN": [103.8354, 1.4304],
    "LIM CHU KANG": [103.7173, 1.4305],
    "TENGAH": [103.7394, 1.3648],
}


def get_download_url():
    url = f"https://api-open.data.gov.sg/v1/public/api/datasets/{DATASET_ID}/initiate-download"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["data"]["url"]


def download_csv(url):
    print("Downloading CSV...")
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    print(f"Downloaded {len(raw) // 1024} KB")
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)


def load_coords_cache():
    if os.path.exists(COORDS_CACHE):
        with open(COORDS_CACHE) as f:
            return json.load(f)
    # Migrate from old street_coords.json if it exists
    if os.path.exists("street_coords.json"):
        print("Migrating street_coords.json as seed for block geocoding...")
        with open("street_coords.json") as f:
            return json.load(f)
    return {}


def save_coords_cache(cache):
    with open(COORDS_CACHE, "w") as f:
        json.dump(cache, f)


def geocode_address(block, street_name):
    """Geocode a block address using Mapbox Geocoding API."""
    query = f"{block} {street_name} SINGAPORE"
    encoded = urllib.parse.quote(query)
    url = (
        f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded}.json"
        f"?country=SG&access_token={MAPBOX_TOKEN}&limit=1"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("features"):
            return data["features"][0]["geometry"]["coordinates"]  # [lng, lat]
    except Exception as e:
        print(f"  Geocode failed for '{query}': {e}")
    return None


def geocode_all_blocks(records):
    """Geocode all unique (block, street) combos, using cache."""
    cache = load_coords_cache()

    # Collect unique addresses with their town (for fallback)
    addresses = {}  # key -> (block, street, town)
    for r in records:
        town = r.get("town", "").strip().upper()
        street = r.get("street_name", "").strip()
        block = r.get("block", "").strip()
        key = f"{block}|{street}"
        if key not in addresses:
            addresses[key] = (block, street, town)

    to_geocode = {k: v for k, v in addresses.items() if k not in cache}
    print(f"Blocks: {len(addresses)} total, {len(cache)} cached, {len(to_geocode)} to geocode")

    if to_geocode:
        failed = 0
        for i, (key, (block, street, town)) in enumerate(to_geocode.items()):
            coords = geocode_address(block, street)
            if coords:
                cache[key] = coords
            else:
                # Fallback: try street-level key from old cache
                street_key = f"{town}|{street}"
                if street_key in cache:
                    cache[key] = cache[street_key]
                elif town in TOWN_COORDS:
                    cache[key] = TOWN_COORDS[town]
                failed += 1

            if (i + 1) % 100 == 0:
                print(f"  Geocoded {i + 1}/{len(to_geocode)} (failed: {failed})...")
                save_coords_cache(cache)

            # Rate limit: ~10 requests/sec
            if (i + 1) % 10 == 0:
                time.sleep(1)

        save_coords_cache(cache)
        print(f"Geocoding complete. {len(cache)} entries cached, {failed} used fallback.")

    return cache


def aggregate(records, coords_cache):
    """Group by (block, street, month, flat_type) and compute average price + count."""
    groups = defaultdict(lambda: {"total_price": 0, "count": 0, "town": ""})

    for r in records:
        town = r.get("town", "").strip().upper()
        street = r.get("street_name", "").strip()
        block = r.get("block", "").strip()
        month = r.get("month", "").strip()
        flat_type = r.get("flat_type", "").strip()
        try:
            price = float(r.get("resale_price", 0))
        except (ValueError, TypeError):
            continue

        key = f"{block}|{street}"
        if not town or not month or key not in coords_cache:
            continue

        group_key = (block, street, month, flat_type)
        groups[group_key]["total_price"] += price
        groups[group_key]["count"] += 1
        groups[group_key]["town"] = town

    return groups


def to_geojson(groups, coords_cache):
    features = []
    for (block, street, month, flat_type), stats in groups.items():
        key = f"{block}|{street}"
        coords = coords_cache.get(key)
        if not coords:
            continue

        avg_price = round(stats["total_price"] / stats["count"])
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": coords,
            },
            "properties": {
                "town": stats["town"],
                "street": street,
                "block": block,
                "month": month,
                "flat_type": flat_type,
                "avg_price": avg_price,
                "count": stats["count"],
            },
        })

    return {"type": "FeatureCollection", "features": features}


def build_transactions(records, coords_cache):
    """Build transactions index keyed by 'BLOCK|STREET' for detail lookups."""
    tx_index = defaultdict(list)
    for r in records:
        street = r.get("street_name", "").strip()
        block = r.get("block", "").strip()
        key = f"{block}|{street}"
        if key not in coords_cache:
            continue
        try:
            price = int(float(r.get("resale_price", 0)))
        except (ValueError, TypeError):
            continue
        tx_index[key].append({
            "m": r.get("month", "").strip(),
            "ft": r.get("flat_type", "").strip(),
            "sr": r.get("storey_range", "").strip(),
            "a": r.get("floor_area_sqm", "").strip(),
            "p": price,
            "fm": r.get("flat_model", "").strip(),
            "lc": r.get("lease_commence_date", "").strip(),
        })
    return tx_index


def main():
    global MAPBOX_TOKEN

    if len(sys.argv) < 2:
        print("Usage: python3 fetch_data.py <MAPBOX_ACCESS_TOKEN>")
        sys.exit(1)

    MAPBOX_TOKEN = sys.argv[1]

    print("Getting download URL from data.gov.sg...")
    download_url = get_download_url()

    records = download_csv(download_url)
    print(f"Total records: {len(records)}")

    print("\nGeocoding blocks...")
    coords_cache = geocode_all_blocks(records)

    print("\nAggregating by block / street / month / flat type...")
    groups = aggregate(records, coords_cache)
    print(f"Aggregated into {len(groups)} groups")

    geojson = to_geojson(groups, coords_cache)

    out_path = "data.json"
    with open(out_path, "w") as f:
        json.dump(geojson, f)
    size_kb = round(os.path.getsize(out_path) / 1024)
    print(f"Wrote {out_path} ({size_kb} KB, {len(geojson['features'])} features)")

    print("\nBuilding transactions index...")
    tx_index = build_transactions(records, coords_cache)
    tx_path = "transactions.json"
    with open(tx_path, "w") as f:
        json.dump(tx_index, f)
    tx_size_kb = round(os.path.getsize(tx_path) / 1024)
    print(f"Wrote {tx_path} ({tx_size_kb} KB, {len(tx_index)} blocks)")


if __name__ == "__main__":
    main()
