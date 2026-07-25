import sys
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "versions"
sys.path.insert(0, str(VERSIONS_DIR))
