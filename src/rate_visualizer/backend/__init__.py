"""The website's backend. The frontend imports only from here:

    from rate_visualizer.backend import Filters, open_backend
    backend = open_backend()                 # downloads/verifies site.duckdb (or uses SITE_DB_PATH)
    backend.code_summary(Filters(codes=("90837",)))

Nothing in this package imports UI code. See docs/backend_api.md for every method.
"""
from .filters import Filters
from .loader import SiteDataError, ensure_site_db
from .queries import Backend
from .settings import Settings


def open_backend(settings=None, store=None, log=print, warm=True):
    """Get a verified site.duckdb (download from R2 if needed) and return a ready Backend."""
    settings = settings or Settings.from_env()
    path, manifest = ensure_site_db(settings, store=store, log=log)
    backend = Backend(path, memory_limit=settings.duckdb_memory_limit, threads=settings.duckdb_threads)
    backend.manifest = manifest
    if warm:
        backend.warm_up()
    return backend


__all__ = ["Backend", "Filters", "Settings", "SiteDataError", "ensure_site_db", "open_backend"]
