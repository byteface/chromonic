"""Load Chromonic's temporary Domonic compatibility snapshot.

Domonic remains the authoritative DOM/CSSOM.  These modules are copied from
the pending upstream checkout so Chromonic can use the fixes before the next
Domonic release.  A narrow import finder substitutes only the four modules in
the snapshot and leaves the rest of the installed Domonic package untouched.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
from pathlib import Path
import sys


_VENDOR_ROOT = Path(__file__).with_name("_vendor") / "domonic"
_MODULES = {
    "domonic._cssom": _VENDOR_ROOT / "_cssom.py",
    "domonic.dom": _VENDOR_ROOT / "dom.py",
    "domonic.layout": _VENDOR_ROOT / "layout.py",
    "domonic.style": _VENDOR_ROOT / "style.py",
}


class _DomonicSnapshotFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        source = _MODULES.get(fullname)
        if source is None:
            return None
        return importlib.util.spec_from_file_location(fullname, source)


_FINDER = _DomonicSnapshotFinder()


def install() -> bool:
    """Install the narrow import override once, before Domonic is imported.

    Already-imported modules retain their class identities.  This matters for
    applications which construct a Domonic tree before importing Chromonic;
    those applications already selected their Domonic implementation and must
    not have it replaced underneath live nodes.
    """
    if _FINDER in sys.meta_path:
        return False
    sys.meta_path.insert(0, _FINDER)
    return True


def active_sources() -> dict[str, str]:
    """Return the source selected for each loaded compatibility module."""
    return {
        name: str(getattr(module, "__file__", ""))
        for name in _MODULES
        if (module := sys.modules.get(name)) is not None
    }

