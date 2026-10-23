"""Compatibility entry point for the renamed PlanetBaltan bridge."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("planetbaltan_tool.py")), run_name="__main__")
