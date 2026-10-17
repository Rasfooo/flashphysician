"""FlashPhysician -- diagnose, repair and recover removable storage.

The public surface is small on purpose. Everything is reachable through
`flashphysician` (CLI) and `flashphysician.gui` (desktop), and both are thin layers over
`core` (what is wrong) and `recovery` (getting data back).

Quick start from Python:

    from flashphysician import diagnose_device, format_report

    report = diagnose_device("/dev/sdb")
    print(format_report(report))
"""

__version__ = "1.0.1"
__author__ = "Rasfooo"
__license__ = "MIT"
__url__ = "https://github.com/Rasfooo/flashphysician"

__all__ = [
    "__version__",
    "diagnose_device",
    "format_report",
    "Severity",
]


def diagnose_device(path: str, progress=None):
    """Read-only diagnosis of one device or image. Convenience wrapper so
    callers do not need to know the module layout."""
    from .core.diagnose import quick_diagnose
    return quick_diagnose(path, progress=progress)


def format_report(report, color: bool = False) -> str:
    from .core.diagnose import format_report as _fmt
    return _fmt(report, color=color)


def __getattr__(name: str):
    # keep heavy imports out of `import flashphysician`
    if name == "Severity":
        from .core.diagnose import Severity
        return Severity
    if name in ("quick_diagnose", "Report"):
        from .core import diagnose as _d
        return getattr(_d, name)
    if name in ("repair_plans", "RepairPlan"):
        from .core import repair as _r
        return getattr(_r, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")