"""publish-site: upload site.duckdb to R2, write latest.json, keep the last N months, call Render's deploy hook.

Order matters: the database is uploaded and verified first, latest.json is written last, so the site never points
at a half-uploaded file.
"""
import datetime, json, os
import requests
from ..backend.loader import sha256_file
from ..backend.storage import store_from_settings
from ..io import run_dir


class PublishError(SystemExit):
    pass


def publish(cfg, index_date, settings, store=None, deploy=True, dry_run=False, log=print):
    """Returns the manifest dict that was (or, with dry_run, would be) written."""
    path = os.path.join(run_dir(cfg, index_date), "site.duckdb")
    if not os.path.exists(path):
        raise PublishError(f"{path} not found - run build-site first")
    key = f"{cfg.r2_prefix}/{index_date}/site.duckdb"
    manifest_key = f"{cfg.r2_prefix}/latest.json"
    from ..extract import git_state
    commit, _ = git_state()
    manifest = {"state": cfg.state, "index_date": index_date, "key": key, "sha256": sha256_file(path),
                "bytes": os.path.getsize(path),
                "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "parser_git_commit": commit}
    if dry_run:
        log(f"dry run: would upload {path} -> {key} and write {manifest_key}")
        return manifest
    store = store or store_from_settings(settings)
    log(f"uploading {path} ({manifest['bytes'] / 2**20:,.0f} MB) -> {key}")
    store.upload_file(path, key)
    remote = store.size(key)
    if remote != manifest["bytes"]:
        raise PublishError(f"uploaded size {remote} != local {manifest['bytes']}; latest.json not updated")
    store.put_bytes(manifest_key, json.dumps(manifest, indent=2).encode(), content_type="application/json")
    log(f"wrote {manifest_key} -> {index_date}")
    # keep the newest N months (by the date folder in the key)
    months = sorted({k.split("/")[-2] for k in store.list(cfg.r2_prefix + "/") if k.endswith("/site.duckdb")})
    old = months[:-cfg.site_months_kept] if len(months) > cfg.site_months_kept else []
    if old:
        doomed = [k for k in store.list(cfg.r2_prefix + "/") if k.split("/")[-2] in old]
        store.delete(doomed)
        log(f"deleted {len(old)} old month(s): {old}")
    if deploy:
        if not settings.render_deploy_hook_url:
            log("RENDER_DEPLOY_HOOK_URL not set: restart the Render service yourself to load the new data")
        else:
            r = requests.post(settings.render_deploy_hook_url, timeout=60)
            if r.status_code >= 300:
                raise PublishError(f"deploy hook returned HTTP {r.status_code}; data is published, restart Render manually")
            log("Render deploy hook called: the site restarts with the new data")
    return manifest
