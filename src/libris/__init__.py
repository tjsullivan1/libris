"""Libris - track the books you have read, are reading, or mean to read."""

__all__ = ["installed_version"]


def installed_version() -> str:
    """The version of the installed libris distribution.

    Read from package metadata rather than a constant, so it cannot disagree
    with what was actually installed - which is the failure this exists to make
    visible. `uv` keys its build cache on the version, so two builds sharing one
    version are indistinguishable until something is missing.

    `importlib.metadata` is imported here rather than at module scope. It costs
    ~186ms to import, and importing the package would otherwise mean reading the
    package's own metadata off disk before any command had been chosen - a cost
    every command paid so that one of them could print a version string (#106).

    Returns:
        The version string, or "unknown" when libris is not installed as a
        distribution, as when running from a source checkout without an install.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as distribution_version

    try:
        return distribution_version("libris")
    except PackageNotFoundError:
        return "unknown"
