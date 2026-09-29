"""Settings from environment variables (and a local .env file, which never overrides real environment variables).

Every variable is listed in .env.example and docs/rates_tool_hosting_plan.md section 10.2.
"""
import os
from dataclasses import dataclass


def load_dotenv(path=".env"):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Existing env vars win."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() and value:
                os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "rates-tool-data"
    site_manifest_key: str = "sites/TX/latest.json"
    render_deploy_hook_url: str = ""
    site_db_path: str = ""            # use this local site.duckdb instead of downloading (development)
    site_cache_dir: str = ".site_cache"
    site_password: str = ""
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 0           # 0 = DuckDB default (all cores)

    @classmethod
    def from_env(cls, dotenv_path=".env"):
        load_dotenv(dotenv_path)
        env = os.environ.get
        return cls(r2_account_id=env("R2_ACCOUNT_ID", ""), r2_access_key_id=env("R2_ACCESS_KEY_ID", ""),
                   r2_secret_access_key=env("R2_SECRET_ACCESS_KEY", ""),
                   r2_bucket=env("R2_BUCKET", "rates-tool-data"),
                   site_manifest_key=env("SITE_MANIFEST_KEY", "sites/TX/latest.json"),
                   render_deploy_hook_url=env("RENDER_DEPLOY_HOOK_URL", ""),
                   site_db_path=env("SITE_DB_PATH", ""), site_cache_dir=env("SITE_CACHE_DIR", ".site_cache"),
                   site_password=env("SITE_PASSWORD", ""),
                   duckdb_memory_limit=env("DUCKDB_MEMORY_LIMIT", "1GB"),
                   duckdb_threads=int(env("DUCKDB_THREADS", "0") or 0))

    def missing_r2(self):
        return [name for name, value in (("R2_ACCOUNT_ID", self.r2_account_id),
                                         ("R2_ACCESS_KEY_ID", self.r2_access_key_id),
                                         ("R2_SECRET_ACCESS_KEY", self.r2_secret_access_key),
                                         ("R2_BUCKET", self.r2_bucket)) if not value]

    def __repr__(self):  # never print secrets
        hidden = {"r2_access_key_id", "r2_secret_access_key", "render_deploy_hook_url", "site_password"}
        shown = ", ".join(f"{k}={'***' if k in hidden and v else v!r}" for k, v in self.__dict__.items())
        return f"Settings({shown})"
