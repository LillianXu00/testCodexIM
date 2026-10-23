"""Compatibility import for planetbaltan.config."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.config")
sys.modules[__name__] = _module
