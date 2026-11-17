from pathlib import Path
import runpy
import sys

APP_DIR = (
    Path(__file__).resolve().parent
    / "nba_quant_engine_collector"
    / "nba_quant_engine"
)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

runpy.run_path(str(APP_DIR / "streamlit_app.py"), run_name="__main__")
