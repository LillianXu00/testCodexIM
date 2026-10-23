"""Compatibility import for planetbaltan.cli."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.cli")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
