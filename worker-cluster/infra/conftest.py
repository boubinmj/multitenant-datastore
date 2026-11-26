import sys
from pathlib import Path

infra_dir = str(Path(__file__).resolve().parent)
if infra_dir not in sys.path:
    sys.path.insert(0, infra_dir)
