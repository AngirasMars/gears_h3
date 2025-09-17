import os, json, time
from datetime import timedelta, datetime
import pandas as pd
import streamlit as st
import pydeck as pdk
import h3  # v4.x

# ===========================
#   UI BASICS
# ===========================
st.set_page_config(page_title="GEARS H3 Demo – FIU", layout="wide")
st.title("GEARS (H3) — Arrivals / Departures / Anomalies")
st.caption("• Red hexes = POI entrance cells (k-ring) • Dots = breadcrumbs • Green = ARRIVE • Yellow = DEPART • Pink = ANOMALY")

# ---------------------------
#   SIDEBAR CONTROLS
# ---------------------------
st.sidebar.header("Data & Map")

default_token = os.getenv("MAPBOX_API_KEY") or os.getenv("MAPBOX_TOKEN") or ""
mapbox_token = st.sidebar.text_input("Mapbox Token (optional)", value=default_token, type="password")
if mapbox_token:
    pdk.settings.mapbox_api_key = mapbox_token

gps_path    = st.sidebar.text_input("GPS (.csv)",      "data/gps.csv")
pois_path   = st.sidebar.text_input("POIs (.geojson)","data/pois.geojson")
events_path = st.sidebar.text_input("Events (.csv)", "out/events_h3.csv")

h3_res = st.sidebar.slider("H3 resolution", 7, 11, 9)
k_ring = st.sidebar.slider("POI k-ring (extra hex layers around entrance)", 0, 2, 1)  # 0 = only entrance cell
show_labels = st.sidebar.checkbox("Show POI labels", value=True)

# ===========================
#   LOAD DATA (BOM-SAFE)
# ===========================
@st.cache_data
def load_data(gps_path, pois_path, events_path, h3_res):
    gps = pd.read_csv(gps_path)
    gps["ts"] = pd.to_datetime(gps["ts"], errors="coerce")

    # BOM-safe read for Windows-created JSON
    with open(pois_path, "r", encoding="utf-8-sig") as f:
        gj = json.loads(f.read())

    pois = []
    for ft in gj["features"]:
        lon, lat = ft["geometry"]["coordinates"]
        props = ft["properties"]
        cell = h3.latlng_to_cell(lat, lon, h3_res)
        pois.append({
            "poi_id": props.get("poi_id",""),
            "name": props.get("name",""),
            "lat": lat, "lon": lon,
            "h3_cell": cell
        })
    pois = pd.DataFrame(pois)

    events = pd.read_csv(events_path) if os.path.exists(events_path) else pd.DataFrame()
    for c in ("ts_event","ts_arrive","ts_depart"):
        if c in events.columns:
            events[c] = pd.to_datetime(events[c], errors="coerce")

    return gps, pois, events

gps, pois, events = load_data(gps_path, pois_path, events_path, h3_res)

# ===========================
#   FILTERS (vehicles)
# ===========================
veh_all = sorted(
    gps["vehicle_id"].astype(str).unique().tolist(),
    key=lambda s: int(s[1:]) if s.lower().startswith("v") and s[1:].isdigit() else s
)
selected_veh = st.sidebar.multiselect("Vehicles", veh_all, default=veh_all)

# ===========================
#   TIME CONTROLS (single time + play)
# ===========================
if len(gps):
    tmin, tmax = gps["ts"].min().to_pydatetime(), gps["ts"].max().to_pydatetime()
else:
    tmin = tmax = datetime.now()

# session state init
if "current_time" not in st.session_state:
    st.session_state.current_time = tmin
if "current_time_slider" not in st.session_state:
    st.session_state.current_time_slider = st.session_state.current_time
if "is_playing" not in st.session_state:
    st.session_state.is_playing = False

st.sidebar.subheader("Time Controls")
trail_minutes = st.sidebar.slider("Trail length (minutes)", 1, 60, 10)
step_seconds  = st.sidebar.select_slider("Play step (seconds/frame)", options=[5,10,15,30,60], value=10)

# Play/Reset FIRST (before slider), so we can update session_state safely
cA, cB = st.sidebar.columns([1,1])
with cA:
    play_val = st.toggle("▶ Play", value=st.session_state.is_playing, key="play_toggle")
    st.session_state.is_playing = play_val
with cB:
    if st.button("⟲ Reset"):
        st.session_state.current_time = tmin
        st.session_state.current_time_slider = tmin
        st.session_state.is_playing = False
        st.rerun()

# Advance time BEFORE we instantiate the slider
if st.session_state.is_playing:
    new_time = st.session_state.current_time + timedelta(seconds=step_seconds)
    if new_time > tmax:
        st.session_state.is_playing = False
    else:
        st.session_state.current_time = new_time
        st.session_state.current_time_slider = new_time
    time.sleep(0.25)   # smoother frames
    st.rerun()

# When the slider moves, sync it back to our internal clock
def _sync_from_slider():
    st.session_state.current_time = st.session_state.current_time_slider

# Exact-time slider (now safe to render)
current_time = st.sidebar.slider(
    "Current time",
    min_value=tmin, max_value=tmax,
    value=st.session_state.current_time,
    step=timedelta(seconds=step_seconds),
    format="YYYY-MM-DD HH:mm:ss",
    key="current_time_slider",
    on_change=_sync_from_slider
)

st.caption(
    f"**Window head**: {st.session_state.current_time.strftime('%Y-%m-%d %H:%M:%S')}   •   "
    f"Trail: last {trail_minutes} min"
)

# ===========================
#   KPI (scoped to vehicles)
# ===========================
k1, k2, k3, k4 = st.columns(4)
k1.metric("Vehicles", len(selected_veh))
k2.metric("POIs", len(pois))
trail_start = st.session_state.current_time - timedelta(minutes=trail_minutes)
crumbs_slice = gps[(gps["ts"] >= trail_start) & (gps["ts"] <= st.session_state.current_time)]
k3.metric("Breadcrumbs (shown)", len(crumbs_slice))
if "dwell_seconds" in events.columns:
    avg_dwell = events.query("event_type.str.contains('DWELL')", engine="python")["dwell_seconds"].mean()
    k4.metric("Avg Dwell (min)", f"{(avg_dwell or 0)/60:.1f}")
else:
    k4.metric("Avg Dwell (min)", "—")

# ===========================
#   BUILD H3 POLYGONS (k-ring) + sanitize
# ===========================
def hex_polygon(cell):
    boundary_latlng = h3.cell_to_boundary(cell)  # [(lat,lon), ...]
    return [[lon, lat] for (lat, lon) in boundary_latlng]

poly_features = []
for _, r in pois.iterrows():
    cells = list(h3.grid_disk(r["h3_cell"], k_ring)) if k_ring > 0 else [r["h3_cell"]]
    for c in cells:
        poly_features.append({
            "name": r["name"],
            "poi_id": r["poi_id"],
            "h3_cell": c,
            "polygon": hex_polygon(c),
        })

# ✅ keep only well-formed polygons
poly_features = [
    pf for pf in poly_features
    if isinstance(pf.get("polygon"), list)
    and len(pf["polygon"]) >= 3
    and all(
        isinstance(pt, list) and len(pt) == 2 and pd.notna(pt[0]) and pd.notna(pt[1])
        for pt in pf["polygon"]
    )
]

# ✅ guard against empty list (deck.gl expects at least a valid schema)
if len(poly_features) > 0:
    poly_layer = pdk.Layer(
        "PolygonLayer",
        data=poly_features,
        get_polygon="polygon",
        get_fill_color=[200, 0, 0, 60],
        get_line_color=[200, 0, 0, 170],
        line_width_min_pixels=2,
        pickable=True,
    )
else:
    poly_layer = pdk.Layer(
        "PolygonLayer",
        data=pd.DataFrame(columns=["polygon", "poi_id", "name"]),
        get_polygon="polygon",
        get_fill_color=[200, 0, 0, 60],
        get_line_color=[200, 0, 0, 170],
        line_width_min_pixels=2,
        pickable=True,
    )

# ===========================
#   EVENTS (up to current time)
# ===========================

# Events table: fail-soft + clean index
if os.path.exists(events_path):
    events = pd.read_csv(events_path)
    if not events.empty:
        if "ts_event" in events.columns:
            events["ts_event"] = pd.to_datetime(events["ts_event"], errors="coerce")
            events = events.sort_values(["ts_event", "vehicle_id", "event_type"]).reset_index(drop=True)
        else:
            events = events.reset_index(drop=True)
        # Ensure vehicle_id exists as str to avoid UI hiccups
        if "vehicle_id" in events.columns:
            events["vehicle_id"] = events["vehicle_id"].astype(str)
else:
    events = pd.DataFrame(columns=[
        "event_type","vehicle_id","poi_id","h3_cell",
        "ts_event","ts_arrive","ts_depart","dwell_seconds",
        "lat","lon","event_id"
    ])

events = events[events["vehicle_id"].isin(selected_veh)]
events["ts_use"] = events["ts_event"].fillna(events.get("ts_arrive"))
events_up_to_now = events[(events["ts_use"].notna()) & (events["ts_use"] <= st.session_state.current_time)]

arrivals = events_up_to_now[events_up_to_now["event_type"] == "ARRIVE"].merge(
    pois[["poi_id", "lon", "lat", "name"]], on="poi_id", how="left"
)
arrivals["ts_str"] = arrivals["ts_use"].dt.strftime("%Y-%m-%d %H:%M:%S")
arrivals["label"]  = "ARRIVE"

departs = events_up_to_now[events_up_to_now["event_type"] == "DEPART"].merge(
    pois[["poi_id", "lon", "lat", "name"]], on="poi_id", how="left"
)
departs["ts_str"] = departs["ts_use"].dt.strftime("%Y-%m-%d %H:%M:%S")
departs["label"]  = "DEPART"

# anomalies: center of anomaly cell
if "event_type" in events_up_to_now.columns:
    anom = events_up_to_now[events_up_to_now["event_type"].str.startswith("ANOMALY", na=False)].copy()
else:
    anom = pd.DataFrame()

if len(anom):
    centers = anom["h3_cell"].apply(lambda c: h3.cell_to_latlng(c) if isinstance(c, str) and len(c) > 0 else None)
    anom["lat"] = centers.apply(lambda x: x[0] if x else None)
    anom["lon"] = centers.apply(lambda x: x[1] if x else None)
    # anom[["lon", "lat"]] = anom[["lat", "lon"]]  # <- REMOVED
    anom["ts_str"] = anom["ts_use"].dt.strftime("%Y-%m-%d %H:%M:%S")
    anom["name"] = "Anomaly"
    anom["label"] = "ANOMALY"

# ===========================
#   LAYERS (alpha-fade + current positions)
# ===========================
palette = [
    [0,122,255],[255,149,0],[52,199,89],[255,59,48],[175,82,222],
    [88,86,214],[255,45,85],[90,200,250],[255,204,0],[50,173,230]
]

gps_trail = gps[(gps["ts"] >= trail_start) & (gps["ts"] <= st.session_state.current_time)].copy()
gps_trail = gps_trail.sort_values(["vehicle_id", "ts"])
if not gps_trail.empty:
    code_map = gps_trail["vehicle_id"].astype("category").cat.codes
    gps_trail["rgb"] = code_map.apply(lambda i: palette[i % len(palette)])

    age_sec = (st.session_state.current_time - gps_trail["ts"]).dt.total_seconds().clip(lower=0)
    trail_sec = max(1.0, trail_minutes * 60.0)
    gps_trail["alpha"] = (255 - (age_sec / trail_sec) * (255-60)).clip(lower=60, upper=255).astype(int)
    gps_trail["color"] = gps_trail.apply(lambda r: [*r["rgb"], int(r["alpha"])], axis=1)
    gps_trail["ts_str"] = gps_trail["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
    gps_trail["name"] = "GPS"

### CHANGE START ###
# Define empty dataframes with correct columns to prevent pydeck errors when data is empty.
# These will be used as fallbacks for the layers.
empty_crumbs = pd.DataFrame(columns=["lon", "lat", "color", "ts_str", "name", "vehicle_id"])
empty_events = pd.DataFrame(columns=["lon", "lat", "label", "vehicle_id", "ts_str"])
empty_current = pd.DataFrame(columns=["lon", "lat", "color", "label", "vehicle_id", "ts_str"])

crumbs = pdk.Layer(
    "ScatterplotLayer",
    data=gps_trail if len(gps_trail) else empty_crumbs,
    get_position=["lon", "lat"],
    get_fill_color="color",
    get_radius=6,
    radius_min_pixels=2,
    pickable=True,
)
### CHANGE END ###

# current position per vehicle (glow)
current_positions = pd.DataFrame() # Initialize as empty
le = gps[gps["ts"] <= st.session_state.current_time].copy()
if len(le):
    idx = le.groupby("vehicle_id")["ts"].idxmax()
    cur = le.loc[idx].copy()
    cur = cur[cur["vehicle_id"].astype(str).isin(selected_veh)]
    if len(cur):
        cur = cur.sort_values("vehicle_id")
        cur["rgb"] = cur["vehicle_id"].astype("category").cat.codes.apply(lambda i: palette[i % len(palette)])
        cur["color"] = cur["rgb"].apply(lambda c: [*c, 255])
        cur["ts_str"] = cur["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        cur["label"] = "CURRENT"
        current_positions = cur

### CHANGE START ###
current_layer = pdk.Layer(
    "ScatterplotLayer",
    data=current_positions if len(current_positions) else empty_current,
    get_position=["lon","lat"],
    get_fill_color="color",
    get_radius=60,
    radius_min_pixels=7,
    stroked=True,
    get_line_color=[255,255,255,240],
    line_width_min_pixels=2,
    pickable=True,
)

arr_layer = pdk.Layer(
    "ScatterplotLayer",
    data=arrivals if len(arrivals) else empty_events,
    get_position=["lon", "lat"],
    get_radius=42,
    radius_min_pixels=6,
    get_fill_color=[0, 200, 0, 210],
    stroked=True,
    get_line_color=[255, 255, 255, 230],
    line_width_min_pixels=1,
    pickable=True,
)

dep_layer = pdk.Layer(
    "ScatterplotLayer",
    data=departs if len(departs) else empty_events,
    get_position=["lon", "lat"],
    get_radius=34,
    radius_min_pixels=5,
    get_fill_color=[255, 200, 0, 210],
    stroked=True,
    get_line_color=[0, 0, 0, 220],
    line_width_min_pixels=1,
    pickable=True,
)
### CHANGE END ###

# This layer already had the correct defensive pattern, which is great!
anom_layer = pdk.Layer(
    "ScatterplotLayer",
    data=anom if len(anom) else pd.DataFrame(columns=["lon","lat"]),
    get_position=["lon", "lat"],
    get_radius=40,
    radius_min_pixels=6,
    get_fill_color=[255, 0, 128, 220],  # magenta
    stroked=True,
    get_line_color=[255, 255, 255, 230],
    line_width_min_pixels=1,
    pickable=True,
)

# ----- POI COUNT LABELS (TextLayer) with guards -----
if len(events_up_to_now):
    counts = events_up_to_now.query("event_type in ['ARRIVE','DEPART']").groupby(["poi_id","event_type"]).size().unstack(fill_value=0).reset_index()
else:
    counts = pd.DataFrame(columns=["poi_id","ARRIVE","DEPART"])

labels = pois.merge(counts, on="poi_id", how="left").fillna(0)
labels = labels[pd.notna(labels["lon"]) & pd.notna(labels["lat"])].copy()
labels["label"] = labels.apply(lambda r: f"{r['name']}: A{int(r.get('ARRIVE',0))} D{int(r.get('DEPART',0))}", axis=1).astype(str)

label_layer = pdk.Layer(
    "TextLayer",
    data=labels,
    get_position=["lon", "lat"],
    get_text="label",
    get_size=16,
    get_color=[255, 255, 255],
    get_alignment_baseline="bottom",
)

# ===========================
#   RENDER MAP
# ===========================
if len(gps_trail):
    mid_lat = float(gps_trail["lat"].mean())
    mid_lon = float(gps_trail["lon"].mean())
else:
    mid_lat, mid_lon = 25.7617, -80.1918

view = pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=14.5, pitch=30)

layers = [poly_layer, crumbs, current_layer, arr_layer, dep_layer, anom_layer]
if show_labels and len(labels):
    layers.append(label_layer)

tooltip = {
    "html": "<b>{label}</b><br/>Veh: {vehicle_id}<br/>{ts_str}",
    "style": {"backgroundColor": "#1f1f1f", "color": "white"}
}

if mapbox_token:
    deck = pdk.Deck(map_style="mapbox://styles/mapbox/dark-v11",
                    initial_view_state=view, layers=layers, tooltip=tooltip)
else:
    base = pdk.Layer("TileLayer", data="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                         min_zoom=0, max_zoom=19, tile_size=256)
    deck = pdk.Deck(map_style=None, initial_view_state=view,
                    layers=[base] + layers, tooltip=tooltip)

tab_map, tab_events = st.tabs(["🗺️ Map", "📒 Events Table"])

with tab_map:
    st.pydeck_chart(deck, use_container_width=True)
    st.caption("Tip: use Play to animate; move the time slider to inspect a moment. Glow = current position per vehicle.")

with tab_events:
    if len(events_up_to_now):
        # Clean table (hide frame index)
        cols = [
            "event_id", "event_type", "vehicle_id", "poi_id",
            "h3_cell", "ts_event", "ts_arrive", "ts_depart", "dwell_seconds"
        ]
        show_cols = [c for c in cols if c in events_up_to_now.columns]
        try:
            st.dataframe(events_up_to_now[show_cols], use_container_width=True, hide_index=True)
        except TypeError:
            # older Streamlit without hide_index
            st.dataframe(events_up_to_now[show_cols].reset_index(drop=True), use_container_width=True)
    else:
        st.info("No events up to the current time / vehicle selection.")

# ===========================
#   LEGEND
# ===========================
with st.expander("Legend / Notes", expanded=False):
    st.markdown("""
- **ARRIVE** is logged when speed ≤ 2 mph for ≥ 200s while staying inside the same H3 cell.
- **Red hexes** show the POI entrance cell expanded by **k-ring** (sidebar control).
- **Dots** = breadcrumbs; **newer dots are more opaque** → direction of travel by recency fade.
- **Glow** = the vehicle’s **current position** at the slider time.
- **Pink** = anomaly stop (outside any POI cell).
""")