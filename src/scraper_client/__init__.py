try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PNF
    __version__: str = _pkg_version("dianshang-scraper-c")
except _PNF:
    __version__ = "0.0.0+local"
