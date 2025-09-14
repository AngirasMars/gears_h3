# GEARS (SVAD → GEARS) Demo

## Quick start
```powershell
.\.venv\Scripts\Activate.ps1
py .\scripts\make_mock.py --out .\data\gps.csv --vehicles 10 --h3-res 9
py .\scripts\run_demo_h3.py --gps .\data\gps.csv --pois .\data\pois.geojson --out .\out\events_h3.csv --h3-res 9 --kring 1 --stop-mph 1.25 --stop-sec 210
streamlit run .\app\streamlit_app.py
