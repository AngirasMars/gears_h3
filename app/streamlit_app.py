# app/streamlit_app.py
# GEARS — stable demo dashboard (safe layers + reliable Play + UX + rich tooltips)

import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import pydeck as pdk
import h3


# ---------------------------
# Page & header
# ---------------------------
st.set_page_config(page_title="GEARS — Fleet Events Demo", layout="wide")
st.title("🚍 GEARS — Fleet Events Demo")
st.caption(
    "Simulated vehicles on roads with ARRIVE/DEPART/DWELL/ANOMALY events. "
    "Red H3 hexes = POI entrance cells (± k-ring). "
    "Green = ARRIVE, Yellow = DEPART, Magenta = ANOMALY. "
    "Hit ▶ Play to watch the day unfold."
)

# ---------------------------
# Sidebar: tokens & controls
# ---------------------------
st.sidebar.header("Settings")

mapbox_token = st.sidebar.text_input(
    "Mapbox token (optional; blank uses OpenStreetMap tiles)",
    value=os.environ.get("MAPBOX_API_KEY", ""),
    type="password",
)
if mapbox_token:
    pdk.settings.mapbox_api_key = mapbox_token

gps_path    = st.sidebar.text_input("GPS CSV",    value="data/gps.csv")
pois_path   = st.sidebar.text_input("POIs GeoJSON", value="data/pois.geojson")
events_path = st.sidebar.text_input("Events CSV", value="out/events_h3.csv")

h3_res  = st.sidebar.slider("H3 resolution (visual only)", 7, 11, 9)
k_ring  = st.sidebar.slider("POI k-ring (visual)", 0, 2, 1)
show_labels = st.sidebar.checkbox("Show POI labels (A/D counts)", value=True)

st.sidebar.subheader("Time Controls")
trail_minutes = st.sidebar.slider("Trail length (minutes)", 1, 60, 12)
step_seconds  = st.sidebar.select_slider("Play step (seconds/frame)", [5, 10, 15, 30, 60], value=10)

# Optional: quick cache clear while developing
if st.sidebar.button("Clear cache"):
    st.cache_data.clear()
    st.rerun()


# ---------------------------
# Data loaders (cached)
# ---------------------------
@st.cache_data(show_spinner=False)
def load_gps(path: str) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=["vehicle_id", "ts", "lat", "lon", "speed_kph"])
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    for c in ["lat", "lon"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["ts", "lat", "lon"]).copy()
    df["vehicle_id"] = df["vehicle_id"].astype(str)
    return df


@st.cache_data(show_spinner=False)
def load_pois(path: str, visual_res: int) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=["poi_id", "name", "lat", "lon", "h3_cell"])
    # utf-8-sig to handle BOM on Windows
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            gj = json.load(f)
    except json.JSONDecodeError:
        with open(path, "r", encoding="utf-8") as f:
            gj = json.load(f)

    feats = []
    for feat in gj.get("features", []):
        props = feat.get("properties", {}) or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords
        if lon is None or lat is None:
            continue
        poi_id = str(props.get("poi_id") or props.get("name") or props.get("id") or "poi")
        name = props.get("name", poi_id)
        cell = h3.latlng_to_cell(float(lat), float(lon), visual_res)
        feats.append({"poi_id": poi_id, "name": name, "lat": float(lat), "lon": float(lon), "h3_cell": cell})
    return pd.DataFrame(feats)


@st.cache_data(show_spinner=False)
def load_events(path: str) -> pd.DataFrame:
    cols = [
        "event_type", "vehicle_id", "poi_id", "h3_cell",
        "ts_event", "ts_arrive", "ts_depart", "dwell_seconds",
        "lat", "lon", "event_id",
    ]
    if not Path(path).exists():
        return pd.DataFrame(columns=cols)
    ev = pd.read_csv(path)
    for c in ["ts_event", "ts_arrive", "ts_depart"]:
        if c in ev.columns:
            ev[c] = pd.to_datetime(ev[c], errors="coerce")
    for c in ["lat", "lon", "dwell_seconds"]:
        if c in ev.columns:
            ev[c] = pd.to_numeric(ev[c], errors="coerce")
    if "vehicle_id" in ev.columns:
        ev["vehicle_id"] = ev["vehicle_id"].astype(str)
    # unified timestamp for display filter
    ev["ts_use"] = ev.get("ts_event")
    if "ts_arrive" in ev.columns:
        ev["ts_use"] = ev["ts_use"].fillna(ev["ts_arrive"])
    return ev


gps    = load_gps(gps_path)
pois   = load_pois(pois_path, h3_res)
events = load_events(events_path)

# Early exit if no GPS
if not len(gps):
    st.warning("No GPS data found. Generate with `scripts/make_mock_osm.py` and then run `scripts/run_demo_h3.py`.")
    st.stop()

# Vehicle filter
all_vehicles = sorted(gps["vehicle_id"].unique().tolist())
vehicles_sel = st.sidebar.multiselect("Vehicles", all_vehicles, default=all_vehicles)
gps = gps[gps["vehicle_id"].isin(vehicles_sel)].copy()
events = events[events["vehicle_id"].isin(vehicles_sel)].copy() if len(events) else events

# Time bounds
tmin = gps["ts"].min().to_pydatetime()
tmax = gps["ts"].max().to_pydatetime()

# Session state init
if "current_time" not in st.session_state:
    st.session_state.current_time = tmin
if "current_time_slider" not in st.session_state:
    st.session_state.current_time_slider = st.session_state.current_time
if "is_playing" not in st.session_state:
    st.session_state.is_playing = False

# Play & Reset
cA, cB = st.sidebar.columns([1, 1])
with cA:
    st.session_state.is_playing = st.toggle("▶ Play", value=st.session_state.is_playing, key="play_toggle")
with cB:
    if st.button("⟲ Reset"):
        st.session_state.current_time = tmin
        st.session_state.current_time_slider = tmin
        st.session_state.is_playing = False
        st.rerun()

# Keep slider in sync (must happen BEFORE widget is rendered)
if st.session_state.is_playing:
    st.session_state.current_time_slider = st.session_state.current_time

def _sync_from_slider():
    st.session_state.current_time = st.session_state.current_time_slider

_ = st.sidebar.slider(
    "Current time",
    min_value=tmin, max_value=tmax,
    value=st.session_state.current_time,
    step=timedelta(seconds=step_seconds),
    format="YYYY-MM-DD HH:mm:ss",
    on_change=_sync_from_slider,
    key="current_time_slider",
)
current_time = st.session_state.current_time  # local alias


# ---------------------------
# Build frame for current_time
# ---------------------------
# Trail slice
trail_start = current_time - timedelta(minutes=trail_minutes)
gps_trail = (
    gps[(gps["ts"] >= trail_start) & (gps["ts"] <= current_time)]
    .copy()
    .sort_values(["vehicle_id", "ts"])
)

# Per-vehicle colors
palette = [
    [255, 99, 132], [54, 162, 235], [255, 206, 86],
    [75, 192, 192], [153, 102, 255], [255, 159, 64],
    [0, 200, 255], [0, 255, 150], [250, 100, 255], [200, 255, 0],
]
veh_ids = sorted(gps_trail["vehicle_id"].unique().tolist()) if len(gps_trail) else []
veh_to_color = {v: palette[i % len(palette)] for i, v in enumerate(veh_ids)}

def _alpha_for(ts_point: pd.Timestamp) -> int:
    # 40..200 alpha based on recency inside trail window
    span = max(1.0, trail_minutes * 60.0)
    secs_ago = max(0.0, (current_time - ts_point).total_seconds())
    w = 1.0 - min(1.0, secs_ago / span)
    return int(40 + w * 160)

# Helper for lat/lon formatting
def _fmt_latlon(lat, lon):
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return "—"

if len(gps_trail):
    gps_trail["color"] = gps_trail.apply(
        lambda r: veh_to_color.get(r["vehicle_id"], [180, 180, 180]) + [_alpha_for(r["ts"])],
        axis=1,
    )
    gps_trail["ts_str"] = gps_trail["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
    # Trail tooltips
    gps_trail["label"] = "Trail • " + gps_trail["vehicle_id"].astype(str)
    gps_trail["tip"] = (
        "Vehicle: " + gps_trail["vehicle_id"].astype(str)
        + "<br/>Time: " + gps_trail["ts_str"]
        + "<br/>Pos: " + gps_trail["lat"].map(lambda x: f"{x:.5f}") + ", "
        + gps_trail["lon"].map(lambda x: f"{x:.5f}")
    )
else:
    gps_trail = pd.DataFrame(columns=["vehicle_id", "ts", "lat", "lon", "color", "ts_str", "label", "tip"])

# Vehicle color legend
with st.sidebar.expander("Legend", expanded=True):
    if len(veh_to_color):
        for vid, rgb in veh_to_color.items():
            r, g, b = rgb
            st.markdown(
                f"<div style='display:flex;align-items:center;margin:2px 0'>"
                f"<div style='width:14px;height:14px;background:rgba({r},{g},{b},1);"
                f"border-radius:3px;margin-right:8px'></div>"
                f"<code>{vid}</code></div>", unsafe_allow_html=True
            )
    else:
        st.caption("No vehicles in window.")

# Current positions (latest per vehicle)
if len(gps_trail):
    idx = gps_trail.groupby("vehicle_id")["ts"].idxmax()
    current_positions = gps_trail.loc[idx, ["vehicle_id", "lat", "lon", "ts", "color"]].copy()
    current_positions["label"] = "Now • " + current_positions["vehicle_id"].astype(str)
    current_positions["ts_str"] = current_positions["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
    current_positions["tip"] = (
        "Current position<br/>Vehicle: " + current_positions["vehicle_id"].astype(str)
        + "<br/>Time: " + current_positions["ts_str"]
        + "<br/>Pos: " + current_positions["lat"].map(lambda x: f"{x:.5f}") + ", "
        + current_positions["lon"].map(lambda x: f"{x:.5f}")
    )
else:
    current_positions = pd.DataFrame(columns=["vehicle_id", "lat", "lon", "ts", "color", "label", "ts_str", "tip"])

# Events up to now
if len(events):
    events_up_to_now = events[(events["ts_use"].notna()) & (events["ts_use"] <= current_time)].copy()
else:
    events_up_to_now = pd.DataFrame(columns=list(events.columns) if len(events) else [])

def _ensure_lonlat(df: pd.DataFrame) -> pd.DataFrame:
    if not len(df):
        return df
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    return df.dropna(subset=["lon", "lat"])

arrivals = _ensure_lonlat(events_up_to_now[events_up_to_now["event_type"] == "ARRIVE"]) if len(events_up_to_now) else pd.DataFrame(columns=["lon", "lat"])
departs  = _ensure_lonlat(events_up_to_now[events_up_to_now["event_type"] == "DEPART"]) if len(events_up_to_now) else pd.DataFrame(columns=["lon", "lat"])
anom     = _ensure_lonlat(events_up_to_now[events_up_to_now["event_type"].astype(str).str.startswith("ANOMALY", na=False)]) if len(events_up_to_now) else pd.DataFrame(columns=["lon", "lat"])

# Tooltip helpers for events
def _evt_time(row):
    t = row.get("ts_event")
    if pd.isna(t):
        if row.get("event_type") == "ARRIVE":
            t = row.get("ts_arrive")
        elif row.get("event_type") == "DEPART":
            t = row.get("ts_depart")
    return t.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(t) else "—"

# Map poi_id -> name for tooltips
poi_name_map = pois.set_index("poi_id")["name"].to_dict() if len(pois) else {}
def _evt_poi(row):
    return poi_name_map.get(row.get("poi_id"), str(row.get("poi_id") or "—"))

# Enrich event frames with label + tip
if len(arrivals):
    arrivals["label"] = "Arrival"
    arrivals["tip"] = arrivals.apply(
        lambda r: f"ARRIVE<br/>POI: {_evt_poi(r)}<br/>Vehicle: {r.get('vehicle_id','')}<br/>Time: {_evt_time(r)}",
        axis=1,
    )
else:
    arrivals = pd.DataFrame(columns=["lon", "lat", "label", "tip"])

if len(departs):
    departs["label"] = "Departure"
    departs["tip"] = departs.apply(
        lambda r: (
            f"DEPART<br/>POI: {_evt_poi(r)}<br/>Vehicle: {r.get('vehicle_id','')}<br/>Time: {_evt_time(r)}"
            + (f"<br/>Dwell: {int(r.get('dwell_seconds',0))} s" if pd.notna(r.get("dwell_seconds")) else "")
        ),
        axis=1,
    )
else:
    departs = pd.DataFrame(columns=["lon", "lat", "label", "tip"])

if len(anom):
    anom["label"] = "Anomaly"
    anom["tip"] = anom.apply(
        lambda r: f"{r.get('event_type','ANOMALY')}<br/>Vehicle: {r.get('vehicle_id','')}"
                  f"<br/>Time: {_evt_time(r)}<br/>Pos: {_fmt_latlon(r.get('lat'), r.get('lon'))}",
        axis=1,
    )
else:
    anom = pd.DataFrame(columns=["lon", "lat", "label", "tip"])

# POI hex set (entrance cell + k-ring) via H3 (no GeoJSON ambiguity)
hex_rows = []
if len(pois):
    for _, r in pois.iterrows():
        cells = list(h3.grid_disk(r["h3_cell"], k_ring)) if k_ring > 0 else [r["h3_cell"]]
        for c in cells:
            hex_rows.append({"poi_id": r["poi_id"], "name": r["name"], "h3_cell": c})
hex_df = pd.DataFrame(hex_rows) if len(hex_rows) else pd.DataFrame(columns=["poi_id", "name", "h3_cell"])

# Labels (ARRIVE/DEPART counts per POI) — explicit dtypes (no FutureWarning)
if len(events_up_to_now):
    counts = (events_up_to_now.query("event_type in ['ARRIVE','DEPART']")
              .groupby(["poi_id","event_type"]).size()
              .unstack(fill_value=0).reset_index())
else:
    counts = pd.DataFrame(columns=["poi_id","ARRIVE","DEPART"])

labels = pois.merge(counts, on="poi_id", how="left") if len(pois) else pd.DataFrame(columns=["poi_id","name","lat","lon","ARRIVE","DEPART"])
for col in ["ARRIVE","DEPART"]:
    if col not in labels.columns:
        labels[col] = 0
labels["ARRIVE"] = pd.to_numeric(labels["ARRIVE"], errors="coerce").fillna(0).astype(int)
labels["DEPART"] = pd.to_numeric(labels["DEPART"], errors="coerce").fillna(0).astype(int)
labels = labels[pd.notna(labels["lon"]) & pd.notna(labels["lat"])].copy()
if len(labels):
    labels["label"] = labels["name"].fillna("") + ": A" + labels["ARRIVE"].astype(str) + " D" + labels["DEPART"].astype(str)
else:
    labels = pd.DataFrame(columns=["lon","lat","label"])

# Enrich hex_df with A/D counts + friendly tips
if len(hex_df):
    cA = labels.set_index("poi_id")["ARRIVE"].to_dict() if "ARRIVE" in labels.columns else {}
    cD = labels.set_index("poi_id")["DEPART"].to_dict() if "DEPART" in labels.columns else {}
    hex_df["ARRIVE"] = hex_df["poi_id"].map(cA).fillna(0).astype(int)
    hex_df["DEPART"] = hex_df["poi_id"].map(cD).fillna(0).astype(int)
    hex_df["label"]  = "POI geofence"
    hex_df["tip"]    = (
        "POI: " + hex_df["name"].astype(str)
        + "<br/>Arrivals: " + hex_df["ARRIVE"].astype(str)
        + " • Departures: " + hex_df["DEPART"].astype(str)
        + f"<br/>This red cell marks the geofenced area used to detect arrivals (k={k_ring})."
    )
else:
    hex_df = pd.DataFrame(columns=["h3_cell","label","tip"])


# ---------------------------
# Layers (ids + schema + visible + autoHighlight)
# ---------------------------
poi_hex_layer = pdk.Layer(
    "H3HexagonLayer",
    id="poi-hexes",
    data=hex_df if len(hex_df) else pd.DataFrame(columns=["h3_cell","label","tip"]),
    get_hexagon="h3_cell",
    get_fill_color=[200, 0, 0, 60],
    get_line_color=[200, 0, 0, 170],
    extruded=False,           # 2D
    elevation_scale=0,
    stroked=True,
    line_width_min_pixels=2,
    pickable=True, autoHighlight=True,
    visible=True,
)

crumbs_layer = pdk.Layer(
    "ScatterplotLayer",
    id="gps-crumbs",
    data=gps_trail if len(gps_trail) else pd.DataFrame(columns=["lon","lat","color","ts_str","vehicle_id","label","tip"]),
    get_position=["lon","lat"],
    get_fill_color="color",
    get_radius=8,
    radius_min_pixels=3,
    pickable=True, autoHighlight=True,
    visible=True,
)

current_layer = pdk.Layer(
    "ScatterplotLayer",
    id="current-pos",
    data=current_positions if len(current_positions) else pd.DataFrame(columns=["lon","lat","color","label","vehicle_id","ts_str","tip"]),
    get_position=["lon","lat"],
    get_fill_color="color",
    get_radius=90,
    radius_min_pixels=8,
    stroked=True,
    get_line_color=[255,255,255,240],
    line_width_min_pixels=2,
    pickable=True, autoHighlight=True,
    visible=True,
)

arr_layer = pdk.Layer(
    "ScatterplotLayer",
    id="arrivals",
    data=arrivals if len(arrivals) else pd.DataFrame(columns=["lon","lat","label","tip"]),
    get_position=["lon","lat"],
    get_radius=42,
    radius_min_pixels=6,
    get_fill_color=[0,200,0,210],
    stroked=True,
    get_line_color=[255,255,255,230],
    line_width_min_pixels=1,
    pickable=True, autoHighlight=True,
    visible=True,
)

dep_layer = pdk.Layer(
    "ScatterplotLayer",
    id="departures",
    data=departs if len(departs) else pd.DataFrame(columns=["lon","lat","label","tip"]),
    get_position=["lon","lat"],
    get_radius=34,
    radius_min_pixels=5,
    get_fill_color=[255,200,0,210],
    stroked=True,
    get_line_color=[0,0,0,220],
    line_width_min_pixels=1,
    pickable=True, autoHighlight=True,
    visible=True,
)

anom_layer = pdk.Layer(
    "ScatterplotLayer",
    id="anomalies",
    data=anom if len(anom) else pd.DataFrame(columns=["lon","lat","label","tip"]),
    get_position=["lon","lat"],
    get_radius=40,
    radius_min_pixels=6,
    get_fill_color=[255,0,128,220],
    stroked=True,
    get_line_color=[255,255,255,230],
    line_width_min_pixels=1,
    pickable=True, autoHighlight=True,
    visible=True,
)

label_layer = pdk.Layer(
    "TextLayer",
    id="poi-labels",
    data=labels if len(labels) and show_labels else pd.DataFrame(columns=["lon","lat","label"]),
    get_position=["lon","lat"],
    get_text="label",
    get_size=14,
    get_color=[255,255,255],
    get_alignment_baseline="bottom",
    pickable=True, autoHighlight=True,
    visible=True,
)

# Optional debug visibility toggles
st.sidebar.subheader("Debug layers")
show_hexes   = st.sidebar.checkbox("POI hexes", True)
show_crumbs  = st.sidebar.checkbox("Crumbs", True)
show_current = st.sidebar.checkbox("Current pos", True)
show_arr     = st.sidebar.checkbox("Arrivals", True)
show_dep     = st.sidebar.checkbox("Departures", True)
show_anom    = st.sidebar.checkbox("Anomalies", True)
show_text    = st.sidebar.checkbox("Labels", True)

poi_hex_layer.visible = show_hexes
crumbs_layer.visible  = show_crumbs
current_layer.visible = show_current
arr_layer.visible     = show_arr
dep_layer.visible     = show_dep
anom_layer.visible    = show_anom
label_layer.visible   = (show_text and show_labels)

layers = [poi_hex_layer, crumbs_layer, current_layer, arr_layer, dep_layer, anom_layer]
if label_layer.visible:
    layers.append(label_layer)

# View state (center on crumbs → else POIs → else 0,0)
if len(gps_trail):
    center_lat = float(gps_trail["lat"].mean())
    center_lon = float(gps_trail["lon"].mean())
elif len(pois):
    center_lat = float(pois["lat"].mean())
    center_lon = float(pois["lon"].mean())
else:
    center_lat, center_lon = 0.0, 0.0

# Optional focus on a single POI
poi_pick = st.sidebar.selectbox("Focus POI", ["(auto)"] + (pois["name"].tolist() if len(pois) else []), index=0)
if poi_pick != "(auto)" and len(pois):
    prow = pois.loc[pois["name"] == poi_pick].head(1)
    if len(prow):
        center_lat = float(prow["lat"].iloc[0])
        center_lon = float(prow["lon"].iloc[0])

view = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=14.5, pitch=0)  # 2D feel (pitch=0)

# Universal tooltip (works for all layers thanks to label + tip fields)
tooltip = {
    "html": (
        "<div style='font-size:12px;line-height:1.3'>"
        "<b>{label}</b><br/>{tip}"
        "</div>"
    ),
    "style": {"backgroundColor": "#111111", "color": "white", "border": "1px solid #666"},
}

# Deck: Mapbox if key present, else OSM tiles
use_mapbox = bool(getattr(pdk.settings, "mapbox_api_key", None))
if use_mapbox:
    deck = pdk.Deck(map_style="mapbox://styles/mapbox/dark-v11",
                    initial_view_state=view, layers=layers, tooltip=tooltip)
else:
    base = pdk.Layer("TileLayer", data="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                     min_zoom=0, max_zoom=19, tile_size=256)
    deck = pdk.Deck(map_style=None,
                    initial_view_state=view, layers=[base] + layers, tooltip=tooltip)

# ---------------------------
# KPIs (event counters)
# ---------------------------
if len(events_up_to_now):
    arr_n  = int((events_up_to_now["event_type"] == "ARRIVE").sum())
    dep_n  = int((events_up_to_now["event_type"] == "DEPART").sum())
    anom_n = int(events_up_to_now["event_type"].astype(str).str.startswith("ANOMALY", na=False).sum())
else:
    arr_n = dep_n = anom_n = 0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Vehicles shown", len(vehicles_sel))
k2.metric("Arrivals (≤ now)", arr_n)
k3.metric("Departures (≤ now)", dep_n)
k4.metric("Anomalies (≤ now)", anom_n)

# ---------------------------
# Map & Events table
# ---------------------------
tab_map, tab_table = st.tabs(["🗺️ Map", "📒 Events Table"])
with tab_map:
    st.pydeck_chart(deck, use_container_width=True)
    st.caption(
        f"**Time**: {current_time.strftime('%Y-%m-%d %H:%M:%S')} • "
        f"Trail: last {trail_minutes} min • Step: {step_seconds}s"
    )

with tab_table:
    if len(events_up_to_now):
        show_cols = ["event_type", "vehicle_id", "poi_id", "ts_event", "ts_arrive", "ts_depart",
                     "dwell_seconds", "lat", "lon", "event_id", "ts_use"]
        show_cols = [c for c in show_cols if c in events_up_to_now.columns]
        st.dataframe(events_up_to_now.sort_values("ts_use").tail(1000)[show_cols],
                     use_container_width=True, height=420)

        # Download filtered events (≤ current time)
        st.download_button(
            "⬇️ Download events ≤ current time",
            data=events_up_to_now[show_cols].to_csv(index=False).encode("utf-8"),
            file_name=f"events_to_{current_time.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("No events yet at this time.")

# ---------------------------
# Next / Prev event jump buttons
# ---------------------------
def _jump_event(curr: datetime, ev: pd.DataFrame, direction: int = +1):
    if not len(ev) or "ts_use" not in ev.columns:
        return None
    ts = ev["ts_use"].dropna().sort_values().values
    if direction > 0:
        later = ts[ts > np.datetime64(curr)]
        return later[0] if len(later) else None
    else:
        earlier = ts[ts < np.datetime64(curr)]
        return earlier[-1] if len(earlier) else None

c1, c2 = st.sidebar.columns(2)
if c1.button("⏮ Prev event"):
    t = _jump_event(current_time, events, direction=-1)
    if t is not None:
        st.session_state.current_time = pd.to_datetime(t).to_pydatetime()
    st.rerun()
if c2.button("⏭ Next event"):
    t = _jump_event(current_time, events, direction=+1)
    if t is not None:
        st.session_state.current_time = pd.to_datetime(t).to_pydatetime()
    st.rerun()

# ---------------------------
# PLAY LOOP: one tick per rerun (simple & robust)
# ---------------------------
if st.session_state.is_playing:
    new_time = st.session_state.current_time + timedelta(seconds=step_seconds)
    if new_time > tmax:
        st.session_state.is_playing = False
    else:
        st.session_state.current_time = new_time
    time.sleep(0.20)  # brief delay so you see frames
    st.rerun()
