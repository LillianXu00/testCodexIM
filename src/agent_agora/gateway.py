"""Compatibility import for planetbaltan.gateway."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.gateway")
sys.modules[__name__] = _module
