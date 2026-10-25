import sys
from pathlib import Path

# Make sort_by_genre importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
