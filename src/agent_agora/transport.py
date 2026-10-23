"""Compatibility import for planetbaltan.transport."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.transport")
sys.modules[__name__] = _module
