# scripts/build_osm_graph.py
"""
Builds and saves a drivable road graph around FIU to data/fiu_drive.graphml

Run (once):
  py scripts/build_osm_graph.py --out data/fiu_drive.graphml

You can change the area by adjusting the place or bbox flags below.
"""
import argparse
import osmnx as ox
from pathlib import Path

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/fiu_drive.graphml", help="output graphml path")
    ap.add_argument("--place", default="Florida International University, Miami, Florida, USA",
                    help="geocoder string for the area")
    ap.add_argument("--network-type", default="drive", choices=["drive","drive_service"],
                    help="road types to include")
    return ap.parse_args()

def main():
    args = parse_args()
    outpath = Path(args.out)
    outpath.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading OSM graph for: {args.place}")
    G = ox.graph_from_place(args.place, network_type=args.network_type, simplify=True)
    # optional: project to UTM for nicer lengths/speeds; OSMnx stores lengths anyway
    print(f"Graph: {len(G.nodes)} nodes, {len(G.edges)} edges")
    ox.save_graphml(G, filepath=outpath)
    print(f"Saved: {outpath.resolve()}")

if __name__ == "__main__":
    main()
