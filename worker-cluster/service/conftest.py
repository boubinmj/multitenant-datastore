import sys
from pathlib import Path

service_dir = str(Path(__file__).resolve().parent)
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)
