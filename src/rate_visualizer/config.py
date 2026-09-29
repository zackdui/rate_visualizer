"""Per-state configuration (configs/<state>.toml)."""
import tomllib
from dataclasses import dataclass, field

REQUIRED = ("state", "in_state_filename_marker", "codes", "code_type")


@dataclass(frozen=True)
class Config:
    state: str
    in_state_filename_marker: str
    codes: tuple[str, ...]
    code_type: str
    index_url: str | None = None
    index_path: str | None = None
    scope: str = "in_state"
    data_dir: str = "data"
    retries: int = 3
    max_memory_gb: float = 13.0    # total RAM for all extract workers together (split evenly between them)
    max_storage_gb: float = 13.0   # total size allowed for everything under data_dir
    # site (build-site / publish-site)
    entity_tags_path: str = "configs/entity_tags_tx.csv"
    geo_zcta_url: str = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/"
                         "2025_Gaz_zcta_national.zip")
    geo_crosswalk_url: str = ("https://data.hrsa.gov/DataDownload/GeoCareNavigator/"
                              "ZIP%20Code%20to%20ZCTA%20Crosswalk.xlsx")
    r2_prefix: str = "sites/TX"         # R2 keys: <r2_prefix>/<index_date>/site.duckdb and <r2_prefix>/latest.json
    site_months_kept: int = 3
    site_max_change: float = 0.30       # build-site stops if a code's row count moves more than this vs last month
    source: str = field(default="", compare=False)


def load(path):
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    if not raw.get("index_url") and not raw.get("index_path"):
        raise ValueError(f"{path}: set index_url or index_path")
    if raw.get("scope", "in_state") not in ("in_state", "all"):
        raise ValueError(f"{path}: scope must be 'in_state' or 'all'")
    unknown = set(raw) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    raw["codes"] = tuple(str(c) for c in raw["codes"])
    return Config(source=str(path), **raw)
