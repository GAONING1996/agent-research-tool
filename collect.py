"""Compatibility shim: the implementation lives in research_tool/collect.py."""
import sys

from research_tool import collect as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__" and hasattr(_impl, "main"):
    raise SystemExit(_impl.main())
