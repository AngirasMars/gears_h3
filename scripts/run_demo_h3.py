#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
run_demo_h3.py
Reads GPS breadcrumbs + POIs and emits ARRIVE / DEPART / DWELL / ANOMALY events.

Inputs
------
--gps  data/gps.csv            (ts, lat, lon, vehicle_id, speed_kph[optional])
--pois data/pois.geojson       (Point features, properties: poi_id, name)

Output
------
--out  out/events_h3.csv with columns:
event_type, vehicle_id, poi_id, h3_cell, ts_event, ts_arrive, ts_depart, dwell_seconds, lat, lon, event_id

Usage
-----
py scripts/run_demo_h3.py --gps data/gps.csv --pois data/pois.geojson --out out/events_h3.csv --h3-res 9 --kring 1 --stop-mph 1.25 --stop-sec 210
"""

import argparse
import json
from pathlib import Path

import h3
import numpy as np
import pandas as pd


# ---------------------------
# Argparse
# ---------------------------

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gps", required=True)
    ap.add_argument("--pois", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--h3-res", type=int, default=9)
    ap.add_argument("--kring", type=int, default=1)
    ap.add_argument("--stop-mph", type=float, default=8.0)
    ap.add_argument("--stop-sec", type=int, default=180)
    return ap.parse_args()


# ---------------------------
# Helpers
# ---------------------------

def mph_from_kph(x):
    try:
        return float(x) * 0.621371
    except Exception:
        return np.nan


def compute_speed_mph_if_missing(df: pd.DataFrame) -> pd.DataFrame:
    """
    If speed_kph exists, convert to mph and fill NaNs as 0.0.
    Otherwise, approximate using per-second distance (assumes 1 Hz).
    """
    if "speed_kph" in df.columns:
        df["speed_mph"] = df["speed_kph"].apply(mph_from_kph).fillna(0.0)
        return df

    df = df.sort_values(["vehicle_id", "ts"]).reset_index(drop=True)
    df["speed_mph"] = 0.0
    for _, g in df.groupby("vehicle_id", sort=False):
        idx = g.index
        # shift lat/lon
        lat1 = g["lat"].values[:-1]
        lon1 = g["lon"].values[:-1]
        lat2 = g["lat"].values[1:]
        lon2 = g["lon"].values[1:]
        # rough meters using cos(latitude)
        dlat = (lat2 - lat1) * 111_111.0
        dlon = (lon2 - lon1) * 111_111.0 * np.maximum(0.1, np.cos(np.deg2rad(np.abs(lat1))))
        dist_m = np.hypot(dlat, dlon)
        mph = (dist_m / 1.0) * 2.23694  # m/s -> mph
        mph = np.insert(mph, 0, 0.0)    # first point has no previous hop
        df.loc[idx, "speed_mph"] = mph
    return df


def load_pois(geojson_path: str, res: int, kring: int):
    """
    Load POIs and precompute their entrance cell + kring.
    Handles Windows files saved with a UTF-8 BOM by using 'utf-8-sig'.
    """
    p = Path(geojson_path)
    if not p.exists():
        raise FileNotFoundError(p)

    try:
        # 'utf-8-sig' automatically skips BOM if present
        with open(p, "r", encoding="utf-8-sig") as f:
            gj = json.load(f)
    except json.JSONDecodeError:
        # Fallback without BOM handling (in case file is true utf-8 w/o BOM but other issue)
        with open(p, "r", encoding="utf-8") as f:
            gj = json.load(f)

    pois = []
    for f in gj["features"]:
        props = f.get("properties", {})
        poi_id = props.get("poi_id") or props.get("name") or props.get("id") or "poi"
        name = props.get("name", poi_id)
        lon, lat = f["geometry"]["coordinates"]
        center_cell = h3.latlng_to_cell(lat, lon, res)
        ring = set(h3.grid_disk(center_cell, kring))
        pois.append(
            {
                "poi_id": str(poi_id),
                "name": name,
                "lat": lat,
                "lon": lon,
                "center_cell": center_cell,
                "kring": ring,
            }
        )
    return pois


def assign_h3(df: pd.DataFrame, res: int) -> pd.DataFrame:
    df["h3_cell"] = df.apply(lambda r: h3.latlng_to_cell(r["lat"], r["lon"], res), axis=1)
    return df


def detect_stops_same_cell(df_v: pd.DataFrame, stop_mph: float, stop_sec: int):
    """
    Detect stop spans per vehicle where:
    - speed_mph <= stop_mph
    - staying in the SAME h3_cell continuously
    - duration >= stop_sec (assuming 1Hz sampling)
    Returns list of dicts: {start_idx, end_idx, h3_cell, ts_arrive, ts_depart, dwell_seconds, lat, lon}
    """
    g = df_v.sort_values("ts").reset_index()
    idx = g["index"].values
    cells = g["h3_cell"].values
    speeds = g["speed_mph"].values
    ts = g["ts"].values
    lats = g["lat"].values
    lons = g["lon"].values

    spans = []
    n = len(g)
    i = 0
    while i < n:
        if speeds[i] <= stop_mph:
            start = i
            cell = cells[i]
            # extend while speed OK and cell unchanged
            j = i + 1
            while j < n and speeds[j] <= stop_mph and cells[j] == cell:
                j += 1
            dur = j - start  # seconds at 1Hz
            if dur >= stop_sec:
                start_idx = int(idx[start])
                end_idx = int(idx[j - 1])
                ts_arrive = pd.Timestamp(ts[start])
                ts_depart = pd.Timestamp(ts[j - 1])
                # Representative location: first point in span
                lat_ev = float(lats[start])
                lon_ev = float(lons[start])
                spans.append(
                    dict(
                        start_idx=start_idx,
                        end_idx=end_idx,
                        h3_cell=cell,
                        ts_arrive=ts_arrive,
                        ts_depart=ts_depart,
                        dwell_seconds=int(dur),
                        lat=lat_ev,
                        lon=lon_ev,
                    )
                )
            i = j
        else:
            i += 1
    return spans


def map_stop_to_poi(stop, pois):
    """
    If the stop's h3_cell is inside any POI's kring, return that POI (first match).
    Else return None (anomaly).
    """
    cell = stop["h3_cell"]
    for p in pois:
        if cell in p["kring"]:
            return p
    return None


# ---------------------------
# Main
# ---------------------------

def main():
    args = parse_args()

    gps_path = Path(args.gps)
    if not gps_path.exists():
        raise FileNotFoundError(gps_path)

    df = pd.read_csv(gps_path)
    # Robust typing
    df["ts"] = pd.to_datetime(df["ts"], utc=False, errors="coerce")
    df = df.dropna(subset=["ts", "lat", "lon", "vehicle_id"]).copy()

    df = compute_speed_mph_if_missing(df)
    df = assign_h3(df, args.h3_res)

    pois = load_pois(args.pois, args.h3_res, args.kring)

    events = []

    # Detect stops per vehicle
    for vid, g in df.groupby("vehicle_id", sort=True):
        spans = detect_stops_same_cell(g, stop_mph=args.stop_mph, stop_sec=args.stop_sec)
        for sp in spans:
            poi = map_stop_to_poi(sp, pois)
            if poi is None:
                # anomaly: stopped away from POIs
                events.append(
                    dict(
                        event_type="ANOMALY_STOP",
                        vehicle_id=vid,
                        poi_id="",
                        h3_cell=sp["h3_cell"],
                        ts_event=sp["ts_arrive"].isoformat(),
                        ts_arrive=sp["ts_arrive"].isoformat(),
                        ts_depart=sp["ts_depart"].isoformat(),
                        dwell_seconds=sp["dwell_seconds"],
                        lat=sp["lat"],
                        lon=sp["lon"],
                    )
                )
            else:
                # ARRIVE
                events.append(
                    dict(
                        event_type="ARRIVE",
                        vehicle_id=vid,
                        poi_id=poi["name"],
                        h3_cell=sp["h3_cell"],
                        ts_event=sp["ts_arrive"].isoformat(),
                        ts_arrive=sp["ts_arrive"].isoformat(),
                        ts_depart="",
                        dwell_seconds=sp["dwell_seconds"],
                        lat=sp["lat"],
                        lon=sp["lon"],
                    )
                )
                # DEPART
                events.append(
                    dict(
                        event_type="DEPART",
                        vehicle_id=vid,
                        poi_id=poi["name"],
                        h3_cell=sp["h3_cell"],
                        ts_event=sp["ts_depart"].isoformat(),
                        ts_arrive="",
                        ts_depart=sp["ts_depart"].isoformat(),
                        dwell_seconds=sp["dwell_seconds"],
                        lat=sp["lat"],
                        lon=sp["lon"],
                    )
                )
                # DWELL (optional informational row)
                events.append(
                    dict(
                        event_type="DWELL",
                        vehicle_id=vid,
                        poi_id=poi["name"],
                        h3_cell=sp["h3_cell"],
                        ts_event=sp["ts_depart"].isoformat(),  # could also use midpoint
                        ts_arrive=sp["ts_arrive"].isoformat(),
                        ts_depart=sp["ts_depart"].isoformat(),
                        dwell_seconds=sp["dwell_seconds"],
                        lat=sp["lat"],
                        lon=sp["lon"],
                    )
                )

    # Build DataFrame and write
    if events:
        out_df = pd.DataFrame(events)
        # Sort by event time, then type for consistent ordering
        out_df["ts_event"] = pd.to_datetime(out_df["ts_event"], errors="coerce")
        out_df = out_df.sort_values(["ts_event", "vehicle_id", "event_type"]).reset_index(drop=True)
        # Stable ID
        out_df["event_id"] = (out_df.index + 1).astype(int)
        # Write
        out_df.to_csv(args.out, index=False)
        print(f"Wrote {len(out_df)} events to {args.out}")
    else:
        # Create empty file with header (nice for UI guards)
        cols = [
            "event_type","vehicle_id","poi_id","h3_cell",
            "ts_event","ts_arrive","ts_depart","dwell_seconds",
            "lat","lon","event_id"
        ]
        pd.DataFrame(columns=cols).to_csv(args.out, index=False)
        print(f"Wrote 0 events to {args.out}")


if __name__ == "__main__":
    main()
