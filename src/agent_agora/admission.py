"""Compatibility import for planetbaltan.admission."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.admission")
sys.modules[__name__] = _module
