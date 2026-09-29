"""Get the site database onto local disk: read latest.json, download site.duckdb, verify its SHA-256.

Used by the site at startup (Render) and by anyone running the site locally. SITE_DB_PATH skips all of this.
"""
import hashlib, json, os
from .storage import StorageError, store_from_settings


class SiteDataError(RuntimeError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(store, key):
    raw = store.get_bytes(key)
    if raw is None:
        raise SiteDataError(f"manifest {key} not found in storage - has publish-site run yet?")
    manifest = json.loads(raw)
    for k in ("key", "sha256", "bytes", "index_date"):
        if k not in manifest:
            raise SiteDataError(f"manifest {key} is missing '{k}'")
    return manifest


def ensure_site_db(settings, store=None, log=print):
    """Return (path to a verified local site.duckdb, manifest dict)."""
    if settings.site_db_path:
        if not os.path.exists(settings.site_db_path):
            raise SiteDataError(f"SITE_DB_PATH {settings.site_db_path} does not exist")
        return settings.site_db_path, {"key": None, "source": "SITE_DB_PATH"}
    try:
        store = store or store_from_settings(settings)
    except StorageError as e:
        raise SiteDataError(str(e)) from e
    manifest = read_manifest(store, settings.site_manifest_key)
    local_dir = os.path.join(settings.site_cache_dir, manifest["index_date"])
    local = os.path.join(local_dir, "site.duckdb")
    if os.path.exists(local) and os.path.getsize(local) == manifest["bytes"] and sha256_file(local) == manifest["sha256"]:
        log(f"site data {manifest['index_date']} already cached at {local}")
        return local, manifest
    os.makedirs(local_dir, exist_ok=True)
    part = local + ".part"
    log(f"downloading {manifest['key']} ({manifest['bytes'] / 2**20:,.0f} MB)")
    store.download_file(manifest["key"], part)
    size, sha = os.path.getsize(part), sha256_file(part)
    if size != manifest["bytes"] or sha != manifest["sha256"]:
        os.remove(part)
        raise SiteDataError(f"downloaded {manifest['key']} doesn't match latest.json "
                            f"(size {size} vs {manifest['bytes']}, sha {sha[:12]} vs {manifest['sha256'][:12]})")
    os.replace(part, local)
    return local, manifest
