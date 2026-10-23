"""Compatibility import for planetbaltan.central."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.central")
sys.modules[__name__] = _module
