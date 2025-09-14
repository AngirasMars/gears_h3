# scripts/make_mock.py
import argparse, math, random
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import h3  # NEW: to snap dwells to an H3 entrance cell

random.seed(13)
np.random.seed(13)

# Anchors
FIU = {"lat": 25.7577, "lon": -80.3730}
GC  = {"lat": 25.7560, "lon": -80.3760}   # POI 1
PG5 = {"lat": 25.7585, "lon": -80.3725}   # POI 2

def lerp(a, b, t): return a + (b - a) * t

def make_leg(p1, p2, start_ts, seconds, jitter_m=6.0):
    rows = []
    for i in range(seconds):
        t = 0 if seconds <= 1 else i / (seconds - 1)
        lat = lerp(p1["lat"], p2["lat"], t)
        lon = lerp(p1["lon"], p2["lon"], t)
        # ~meter-level jitter for realism while moving
        lat += np.random.normal(0, jitter_m / 111_111.0)
        lon += np.random.normal(0, jitter_m / (111_111.0 * max(0.1, math.cos(math.radians(abs(lat))))))
        ts  = start_ts + timedelta(seconds=i)
        rows.append((ts, lat, lon))
    return rows

def dwell_h3_center(poi, start_ts, seconds, h3_res=9):
    """
    NEW: create a dwell that stays in the EXACT SAME H3 entrance cell for all 'seconds'.
    This guarantees 'same cell' + ~0 speed for the stop detector.
    """
    # Entrance cell from POI lat/lon at the given resolution
    cell = h3.latlng_to_cell(poi["lat"], poi["lon"], h3_res)
    lat_c, lon_c = h3.cell_to_latlng(cell)
    rows = []
    for i in range(seconds):
        ts = start_ts + timedelta(seconds=i)
        rows.append((ts, lat_c, lon_c))
    return rows

def kph(prev, curr, dt_s):
    dlat = (curr[1] - prev[1]) * 111_111.0
    dlon = (curr[2] - prev[2]) * 111_111.0 * max(0.1, math.cos(math.radians(abs(prev[1]))))
    dist_m = math.hypot(dlat, dlon)
    return (dist_m / dt_s) * 3.6 if dt_s > 0 else 0.0

# relative offsets (≈ 600–1200 m spokes)
OFF = [
    (+0.012,  +0.005), (+0.010, -0.006), (+0.008, +0.012), (-0.010, +0.010), (-0.012, -0.003),
    (+0.005,  -0.012), (-0.007, +0.006), (+0.006, +0.006), (-0.011, -0.010), (+0.013, -0.002),
]

def add_off(p, dlat, dlon): return {"lat": p["lat"] + dlat, "lon": p["lon"] + dlon}

def route_waypoints(idx):
    a = add_off(FIU, *OFF[idx % len(OFF)])
    b = add_off(FIU, *OFF[(idx + 3) % len(OFF)])
    c = add_off(FIU, *OFF[(idx + 6) % len(OFF)])
    if idx % 3 == 0:
        return [FIU, a, GC, PG5, b, FIU]
    elif idx % 3 == 1:
        return [FIU, GC, a, PG5, c, FIU]
    else:
        return [FIU, a, b, GC, PG5, c, FIU]

def simulate_vehicle(vid_str, start_ts, h3_res):
    rows = []
    t = start_ts
    wps = route_waypoints(int(vid_str[1:]) - 1)
    leg_secs = [210, 240, 210, 240, 210, 210]  # visual variety

    for i in range(len(wps) - 1):
        rows += make_leg(wps[i], wps[i+1], t, leg_secs[i % len(leg_secs)])
        t += timedelta(seconds=leg_secs[i % len(leg_secs)])
        here = wps[i+1]
        # If we reached a POI, inject a dwell that stays in the POI's entrance cell
        if abs(here["lat"] - GC["lat"]) < 1e-3 and abs(here["lon"] - GC["lon"]) < 1e-3:
            rows += dwell_h3_center(GC,  t, 240, h3_res=h3_res);  t += timedelta(seconds=240)
        if abs(here["lat"] - PG5["lat"]) < 1e-3 and abs(here["lon"] - PG5["lon"]) < 1e-3:
            rows += dwell_h3_center(PG5, t, 240, h3_res=h3_res); t += timedelta(seconds=240)

    df = pd.DataFrame(rows, columns=["ts","lat","lon"]).sort_values("ts").reset_index(drop=True)
    df["vehicle_id"] = vid_str

    # per-second speed
    spd = [0.0]
    for i in range(1, len(df)):
        spd.append(kph(df.iloc[i-1][["ts","lat","lon"]].values, df.iloc[i][["ts","lat","lon"]].values, 1.0))
    df["speed_kph"] = np.clip(np.asarray(spd, dtype=float), 0, None)

    # hygiene
    df = df.dropna(subset=["lat","lon"])
    df = df[df["lat"].between(-90,90) & df["lon"].between(-180,180)]
    return df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--vehicles", type=int, default=10)
    ap.add_argument("--h3-res", type=int, default=9)  # NEW: match detector resolution
    args = ap.parse_args()

    base = datetime(2025, 1, 1, 8, 0, 0)
    all_df = []
    for v in range(1, args.vehicles + 1):
        vid = f"v{v:02d}"
        start_ts = base + timedelta(seconds=30 * (v-1))  # staggered starts
        all_df.append(simulate_vehicle(vid, start_ts, h3_res=args.h3_res))

    gps = pd.concat(all_df, ignore_index=True).sort_values(["vehicle_id","ts"])
    gps.to_csv(args.out, index=False)
    print(f"Wrote {len(gps)} rows to {args.out} for {args.vehicles} vehicles (h3_res={args.h3_res})")

if __name__ == "__main__":
    main()
