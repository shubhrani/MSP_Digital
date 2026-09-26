"""check_influx.py - diagnose the InfluxDB connection end to end.

Run from workspace root:
    python check_influx.py
"""
import os
import sys

# Ensure Updated_version is in Python path when run from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "Updated_version"))

from check_influx import main

if __name__ == "__main__":
    sys.exit(main() or 0)
