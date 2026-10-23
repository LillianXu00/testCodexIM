"""Compatibility import for planetbaltan.sidecar."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.sidecar")
sys.modules[__name__] = _module
