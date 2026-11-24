"""Tests import the repo-root scripts as modules; nothing here touches the network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
