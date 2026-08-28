"""Libris - track the books you have read, are reading, or mean to read."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

__all__ = ["installed_version"]


def installed_version() -> str:
    """The version of the installed libris distribution.

    Read from package metadata rather than a constant, so it cannot disagree
    with what was actually installed - which is the failure this exists to make
    visible. `uv` keys its build cache on the version, so two builds sharing one
    version are indistinguishable until something is missing.

    Returns:
        The version string, or "unknown" when libris is not installed as a
        distribution, as when running from a source checkout without an install.
    """
    try:
        return _distribution_version("libris")
    except PackageNotFoundError:
        return "unknown"
