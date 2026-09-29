"""Backend: every query the website needs, one method per page section (rates_tool_plan.md section 8).

Rules:
- Inputs are a Filters object plus a few plain arguments; outputs are plain Python data (dicts, lists, str, float,
  int, bool, None) that can go straight to a UI or to JSON. No UI code here, and no SQL in the frontend.
- One read-only DuckDB connection; each call uses its own cursor, so calls are safe from multiple threads.
- Results are cached per (method, arguments); Filters is immutable and hashable, so it can be a cache key.

Counting unit (Filters.counting_unit): "tin" and "npi" first reduce each unit to one value per code (the median of
its rates under the current filters); "row" uses every rate row.
"""
import bisect, collections, datetime, math, statistics, threading
import duckdb
from . import schema as S
from .filters import Filters

EXPLORER_COLUMNS = {  # name -> SQL (whitelist: also the only allowed sort keys)
    "rate_id": "r.rate_id", "billing_code": "r.billing_code", "negotiated_rate": "r.negotiated_rate",
    "negotiated_type": "r.negotiated_type", "rate_unit": "r.rate_unit", "networks": "r.networks",
    "service_code": "ps.service_code", "pos_group": "r.pos_group", "billing_code_modifier": "r.billing_code_modifier",
    "billing_class": "r.billing_class", "expiration_date": "r.expiration_date", "npi": "r.npi",
    "provider_name": "p.provider_name", "provider_type_group": "r.provider_type_group",
    "specialty": "p.specialty_display_name", "tin": "r.tin", "tin_name": "t.display_name",
    "practice_city": "r.practice_city", "practice_state": "r.practice_state", "practice_zip5": "r.practice_zip5",
    "scope_flag": "r.scope_flag", "n_sources": "r.n_sources", "snapshot_date": "r.snapshot_date",
}
SUMMARIZE = {
    "code": ("r.billing_code",),
    "tin_code": ("r.tin", "r.billing_code"),
    "npi_code": ("r.npi", "r.billing_code"),
}
BREAKDOWNS = {None: None, "provider_type": "r.provider_type_group", "network": "network",
              "place_of_service": "r.pos_group", "tag": "tag"}
DQ_KINDS = ("zero_rate", "invalid_npi", "not_in_nppes", "expired", "conflicts")


def _clean(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


class Backend:
    def __init__(self, db_path, memory_limit="1GB", threads=0, cache_size=512):
        cfg = {"memory_limit": memory_limit}
        if threads:
            cfg["threads"] = threads
        self.db_path = db_path
        self._con = duckdb.connect(db_path, read_only=True, config=cfg)
        self._con.execute("SET enable_progress_bar = false")
        self._cache, self._cache_size, self._lock = collections.OrderedDict(), cache_size, threading.Lock()
        meta = self._rows(f"SELECT * FROM {S.T_SITE_META}")
        if not meta:
            raise RuntimeError(f"{db_path} has no {S.T_SITE_META} row")
        self._meta = meta[0]
        if self._meta["schema_version"] != S.SCHEMA_VERSION:
            raise RuntimeError(f"site.duckdb schema {self._meta['schema_version']} != backend {S.SCHEMA_VERSION}; "
                               "rebuild with build-site")

    def warm_up(self):
        """Run the calls every first page view needs, so they're cached before the site reports healthy."""
        f = Filters()
        self.filter_options()
        self.data_quality()
        for breakdown in (None, "provider_type", "network", "place_of_service"):
            self.code_summary(f, breakdown)
        self.benchmarks(f)
        self.rate_type_composition(f)
        self.rate_explorer(f)

    def close(self):
        self._con.close()

    # ---- internals ---------------------------------------------------------------------------------------
    def _rows(self, sql, params=()):
        cur = self._con.cursor()
        try:
            cur.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            return [{c: _clean(v) for c, v in zip(cols, row)} for row in cur.fetchall()]
        finally:
            cur.close()

    def _memo(self, key, fn):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = fn()
        with self._lock:
            self._cache[key] = value
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return value

    @staticmethod
    def _tbl(f, ignore=()):
        """rates_core (pre-filtered to the default toggles, ~10x smaller) when the filters a query applies are at
        least as strict as the defaults; otherwise the full rates table. Both give identical answers."""
        return S.T_RATES_CORE if f is not None and f.fits_core(ignore) else S.T_RATES

    @staticmethod
    def _source(tbl, needs_network):
        return f"(SELECT *, unnest(networks) AS network FROM {tbl})" if needs_network else tbl

    def _units_cte(self, f, group=("r.billing_code",), *, market=True, ignore=(), extra_where="", extra_params=(),
                   needs_network=False):
        """SQL for a CTE body with columns (group..., u, v): one value per counting unit per group."""
        where, params = f.where("r", market=market, ignore=ignore)
        if extra_where:
            where, params = f"{where} AND {extra_where}", params + list(extra_params)
        g = ", ".join(group)
        src = self._source(self._tbl(f, ignore), needs_network)
        if f.counting_unit == "row":
            sql = f"SELECT {g}, r.rate_id AS u, r.negotiated_rate AS v FROM {src} r WHERE {where}"
        else:
            sql = (f"SELECT {g}, {f.unit_key('r')} AS u, median(r.negotiated_rate) AS v FROM {src} r "
                   f"WHERE {where} GROUP BY ALL")
        return sql, params

    def _market(self, units_sql, params):
        """{billing_code: sorted list of unit values} for a units CTE."""
        out = collections.defaultdict(list)
        for code, v in self._con.cursor().execute(
                f"SELECT billing_code, v FROM ({units_sql}) ORDER BY billing_code, v", list(params)).fetchall():
            out[code].append(v)
        return out

    @staticmethod
    def _rank(values, x):
        """(pct_below, pct_at_or_below, n, median) of x within sorted values."""
        n = len(values)
        if not n or x is None:
            return None, None, n, statistics.median(values) if n else None
        return (bisect.bisect_left(values, x) * 100.0 / n, bisect.bisect_right(values, x) * 100.0 / n, n,
                statistics.median(values))

    # ---- general -------------------------------------------------------------------------------------------
    def meta(self):
        return dict(self._meta)

    def health(self):
        try:
            n = self._rows(f"SELECT count(*) AS n FROM {S.T_RATES}")[0]["n"]
            return {"ok": True, "rates": n, "snapshot_date": self._meta["snapshot_date"]}
        except Exception as e:  # noqa: BLE001 - health must never raise
            return {"ok": False, "error": str(e)}

    def filter_options(self):
        """Every option list for the global filter bar (cached for the life of the process)."""
        def build():
            q = self._rows
            return {
                "snapshot_date": self._meta["snapshot_date"],
                "codes": [{"value": r["c"], "label": f"{r['c']} {S.CODE_LABELS.get(r['c'], '')}".strip(),
                           "group": S.CODE_GROUPS.get(r["c"])}
                          for r in q(f"SELECT DISTINCT billing_code AS c FROM {S.T_RATES} ORDER BY 1")],
                "provider_types": [t for t in S.PROVIDER_TYPES if t in {r["g"] for r in q(
                    f"SELECT DISTINCT provider_type_group AS g FROM {S.T_RATES}")}],
                "networks": [r["n"] for r in q(f"SELECT DISTINCT unnest(network_names) AS n FROM {S.T_FILES} ORDER BY 1")],
                "states": q(f"""SELECT practice_state AS value, count(*) AS n_providers FROM {S.T_PROVIDERS}
                                WHERE practice_state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""),
                "cities": q(f"""SELECT practice_city AS value, practice_state AS state, count(*) AS n_providers
                                FROM {S.T_PROVIDERS} WHERE practice_state = 'TX' AND practice_city IS NOT NULL
                                GROUP BY 1, 2 ORDER BY 3 DESC"""),
                "zips": q(f"""SELECT practice_zip5 AS value, any_value(practice_city) AS city, count(*) AS n_providers
                              FROM {S.T_PROVIDERS} WHERE practice_state = 'TX' AND practice_zip5 IS NOT NULL
                              GROUP BY 1 ORDER BY 1"""),
                "places_of_service": [{"value": r["s"], "label": S.POS_LABELS.get(r["s"], r["s"])} for r in
                                      q(f"SELECT DISTINCT unnest(service_code) AS s FROM {S.T_POS_SETS} ORDER BY 1")],
                "pos_groups": [{"value": g, "label": S.POS_GROUP_LABELS[g]} for g in S.POS_GROUPS],
                "billing_classes": [r["b"] for r in q(f"SELECT DISTINCT billing_class AS b FROM {S.T_RATES} ORDER BY 1")],
                "negotiated_types": [r["t"] for r in q(f"SELECT DISTINCT negotiated_type AS t FROM {S.T_RATES} ORDER BY 1")],
                "tag_types": ["platform", "health_system", "none"],
                "tag_names": q(f"""SELECT tag_type, tag_name, count(*) AS n_tins FROM {S.T_ENTITY_TAGS}
                                   GROUP BY 1, 2 ORDER BY 1 DESC, 2"""),
                "scope_flags": [{"value": s, "label": S.SCOPE_LABELS[s]} for s in S.SCOPE_FLAGS],
                "counting_units": ["tin", "npi", "row"],
                "defaults": Filters().to_dict(),
            }
        return self._memo(("filter_options",), build)

    def suggest(self, field, text, limit=20):
        """Type-to-search suggestions. field: 'city' | 'zip' | 'provider' | 'entity'."""
        t = f"%{(text or '').strip().upper()}%"
        sql = {
            "city": f"""SELECT practice_city AS value, practice_state AS state, count(*) AS n FROM {S.T_PROVIDERS}
                        WHERE upper(practice_city) LIKE ? GROUP BY 1, 2 ORDER BY n DESC LIMIT ?""",
            "zip": f"""SELECT practice_zip5 AS value, any_value(practice_city) AS city, count(*) AS n FROM {S.T_PROVIDERS}
                       WHERE practice_zip5 LIKE ? GROUP BY 1 ORDER BY n DESC LIMIT ?""",
            "provider": f"""SELECT npi AS value, provider_name AS label, provider_type_group, practice_city
                            FROM {S.T_PROVIDERS} WHERE npi LIKE ? OR upper(provider_name) LIKE ?
                            ORDER BY provider_name LIMIT ?""",
            "entity": f"""SELECT tin AS value, display_name AS label, n_npis, tag_name FROM {S.T_TINS}
                          WHERE search_text LIKE ? ORDER BY n_npis DESC LIMIT ?""",
        }[field]
        params = [t, t, limit] if field == "provider" else [t, limit]
        return self._rows(sql, params)

    # ---- 8.1 Code Summary -------------------------------------------------------------------------------------
    def code_summary(self, f: Filters, breakdown=None):
        """One row per code (optionally per code x breakdown): counts, percentiles, mean, % percentage rates.

        breakdown: None | 'provider_type' | 'network' | 'place_of_service' | 'tag'.
        """
        if breakdown not in BREAKDOWNS:
            raise ValueError(f"breakdown must be one of {list(BREAKDOWNS)}")
        return self._memo(("code_summary", f, breakdown), lambda: self._code_summary(f, breakdown))

    def _code_summary(self, f, breakdown):
        needs_net = breakdown == "network"
        bexpr = {"tag": f.tag_expr("tag_name", "r")}.get(breakdown, BREAKDOWNS[breakdown])
        group = ["r.billing_code"] + ([f"{bexpr} AS breakdown"] if breakdown else [])
        gnames = ["billing_code"] + (["breakdown"] if breakdown else [])
        gsel = ", ".join(group)
        gby = ", ".join(gnames)
        where, params = f.where("r", market=True)
        units_sql, uparams = self._units_cte(f, group=tuple(group), needs_network=needs_net)
        src = self._source(self._tbl(f), needs_net)
        src_all_units = self._source(self._tbl(f, {"dollar_rates_only"}), needs_net)
        rows = self._rows(f"""
            WITH base AS (SELECT {gsel}, r.tin, r.npi FROM {src} r WHERE {where}),
                 counts AS (SELECT {gby}, count(DISTINCT tin) AS n_tins, count(DISTINCT npi) AS n_npis,
                                   count(*) AS n_rows FROM base GROUP BY {gby}),
                 units AS ({units_sql}),
                 stats AS (SELECT {gby}, count(*) AS n_units, min(v) AS min, max(v) AS max, avg(v) AS mean,
                                  quantile_cont(v, [0.1, 0.25, 0.5, 0.75, 0.9]) AS q FROM units GROUP BY {gby})
            SELECT * FROM counts JOIN stats USING ({gby}) ORDER BY {gby}""", params + uparams)
        # share of percentage rates: same filters, but all rate units
        pw, pp = f.where("r", market=True, ignore={"dollar_rates_only"})
        pct = {tuple(r[g] for g in gnames): r["pct"] for r in self._rows(f"""
            SELECT {gsel}, avg(CASE WHEN r.rate_unit = 'percent_of_billed_charges' THEN 1.0 ELSE 0.0 END) * 100 AS pct
            FROM {src_all_units} r WHERE {pw} GROUP BY {gby}""", pp)}
        out = []
        for r in rows:
            q = r.pop("q") or [None] * 5
            r.update(p10=q[0], p25=q[1], p50=q[2], p75=q[3], p90=q[4], label=S.CODE_LABELS.get(r["billing_code"]),
                     pct_percentage_rows=pct.get(tuple(r[g] for g in gnames)))
            out.append(r)
        return {"counting_unit": f.counting_unit, "breakdown": breakdown, "rows": out}

    def histogram(self, f: Filters, code, bins=30):
        """Distribution of unit values for one code, plus pinned benchmark markers."""
        return self._memo(("histogram", f, code, bins), lambda: self._histogram(f, code, bins))

    def _histogram(self, f, code, bins):
        fc = f.replace(codes=(code,))
        units_sql, params = self._units_cte(fc)
        r = self._rows(f"""WITH units AS ({units_sql})
            SELECT count(*) AS n, quantile_cont(v, 0.01) AS lo, quantile_cont(v, 0.99) AS hi,
                   min(v) AS min, max(v) AS max FROM units""", params)[0]
        markers = self._benchmark_markers(fc, code)
        if not r["n"]:
            return {"code": code, "n": 0, "edges": [], "counts": [], "underflow": 0, "overflow": 0, "markers": markers}
        lo, hi = r["lo"], r["hi"]
        if hi <= lo:
            lo, hi = r["min"], r["max"] if r["max"] > r["min"] else r["min"] + 1
        width = (hi - lo) / bins
        counts = self._rows(f"""WITH units AS ({units_sql})
            SELECT least(greatest(floor((v - ?) / ?), -1), ?)::INT AS b, count(*) AS n FROM units GROUP BY 1""",
                            params + [lo, width, bins])
        hist = [0] * bins
        under = over = 0
        for c in counts:
            if c["b"] < 0:
                under += c["n"]
            elif c["b"] >= bins:
                over += c["n"]
            else:
                hist[c["b"]] += c["n"]
        return {"code": code, "n": r["n"], "counting_unit": f.counting_unit,
                "edges": [lo + i * width for i in range(bins + 1)], "counts": hist, "underflow": under,
                "overflow": over, "min": r["min"], "max": r["max"], "markers": markers}

    # ---- 8.2 Benchmarks -----------------------------------------------------------------------------------------
    def _benchmark_markers(self, f, code):
        tag, ttype = f.tag_expr("tag_name", "r"), f.tag_expr("tag_type", "r")
        where, params = f.where("r", ignore={"tag_types", "tag_names", "exclude_platforms_from_market"})
        return self._rows(f"""SELECT {ttype} AS tag_type, {tag} AS tag_name, median(r.negotiated_rate) AS median_rate,
                                     list(DISTINCT r.negotiated_rate ORDER BY r.negotiated_rate) AS rates
                              FROM {self._tbl(f)} r WHERE {where} AND r.billing_code = ? AND {tag} IS NOT NULL
                              {self._tag_scope(f)} GROUP BY 1, 2 ORDER BY 1 DESC, 2""", params + [code])

    @staticmethod
    def _tag_scope(f):
        """Restrict benchmark entities to the selected tag types/names (if any), via literal-safe SQL."""
        parts = []
        real = [t for t in f.tag_types if t != "none"]
        if real:
            parts.append(f"AND {f.tag_expr('tag_type', 'r')} IN (" + ", ".join("'" + t.replace("'", "''") + "'"
                                                                             for t in real) + ")")
        if f.tag_names:
            parts.append(f"AND {f.tag_expr('tag_name', 'r')} IN (" + ", ".join("'" + t.replace("'", "''") + "'"
                                                                             for t in f.tag_names) + ")")
        return " ".join(parts)

    def benchmarks(self, f: Filters, codes=None):
        """Each tagged entity's distinct rates next to the market, with percentile ranks.

        One line per entity x code x distinct rate x network set x place-of-service group x provider type.
        Market = counting-unit values under the same filters (tag filters don't narrow the market;
        exclude_platforms_from_market does).
        """
        codes = tuple(codes or f.codes or S.CODES)
        return self._memo(("benchmarks", f, codes), lambda: self._benchmarks(f, codes))

    def _benchmarks(self, f, codes):
        fm = f.replace(codes=codes)
        units_sql, uparams = self._units_cte(fm, ignore={"tag_types", "tag_names"})
        market = self._market(units_sql, uparams)
        tag, ttype = f.tag_expr("tag_name", "r"), f.tag_expr("tag_type", "r")
        where, params = fm.where("r", ignore={"tag_types", "tag_names", "exclude_platforms_from_market"})
        rows = self._rows(f"""SELECT * FROM (
            SELECT {ttype} AS tag_type, {tag} AS tag_name, r.billing_code, r.negotiated_rate AS rate,
                   r.networks, r.pos_group, r.provider_type_group,
                   count(DISTINCT r.npi) AS n_npis, count(DISTINCT r.tin) AS n_tins, count(*) AS n_rows
            FROM {self._tbl(fm)} r WHERE {where} AND {tag} IS NOT NULL {self._tag_scope(f)}
            GROUP BY ALL) e
            ORDER BY billing_code, tag_type DESC, tag_name, rate, provider_type_group, pos_group,
                     array_to_string(networks, ',')""", params)
        for r in rows:
            below, at, n, med = self._rank(market.get(r["billing_code"], []), r["rate"])
            r.update(n_market=n, market_median=med, pct_below=below, pct_at_or_below=at)
        return {"counting_unit": f.counting_unit, "rows": rows}

    # ---- 8.3 / 8.4 Rate Explorer and Percentage Rates ----------------------------------------------------------
    def _select_rows(self, where, params, page, page_size, sort_by, sort_dir, f=None):
        if sort_by not in EXPLORER_COLUMNS:
            raise ValueError(f"sort_by must be one of {sorted(EXPLORER_COLUMNS)}")
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        page, page_size = max(1, int(page)), max(1, min(int(page_size), 5000))
        tag = f.tag_expr("tag_name", "r") if f else "r.tag_name"
        tbl = self._tbl(f)
        total = self._rows(f"SELECT count(*) AS n FROM {tbl} r WHERE {where}", params)[0]["n"]
        cols = ", ".join(f"{sql} AS {name}" for name, sql in EXPLORER_COLUMNS.items())
        sort = EXPLORER_COLUMNS[sort_by]
        joins = (f"LEFT JOIN {S.T_PROVIDERS} p ON p.npi = r.npi LEFT JOIN {S.T_TINS} t ON t.tin = r.tin "
                 f"LEFT JOIN {S.T_POS_SETS} ps ON ps.pos_set_id = r.pos_set_id")
        if sort.startswith("r."):  # sort on the rates table alone, then join names for this page only
            ids = [x[0] for x in self._con.cursor().execute(
                f"SELECT r.rate_id FROM {tbl} r WHERE {where} ORDER BY {sort} {direction} NULLS LAST, r.rate_id "
                f"LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size]).fetchall()]
            if not ids:
                return {"total": total, "page": page, "page_size": page_size, "rows": []}
            id_list = ", ".join(str(int(i)) for i in ids)
            rows = self._rows(f"""SELECT {cols}, {tag} AS tag_name FROM {tbl} r {joins}
                WHERE r.rate_id IN ({id_list}) ORDER BY {sort} {direction} NULLS LAST, r.rate_id""")
        else:
            rows = self._rows(f"""SELECT {cols}, {tag} AS tag_name FROM {tbl} r {joins}
                WHERE {where} ORDER BY {sort} {direction} NULLS LAST, r.rate_id
                LIMIT ? OFFSET ?""", params + [page_size, (page - 1) * page_size])
        return {"total": total, "page": page, "page_size": page_size, "rows": rows}

    def rate_explorer(self, f: Filters, page=1, page_size=200, sort_by="negotiated_rate", sort_dir="asc",
                      summarize_by=None):
        """Row-level rates (or summarised: 'code' | 'tin_code' | 'npi_code'), one page at a time."""
        key = ("rate_explorer", f, page, page_size, sort_by, sort_dir, summarize_by)
        if summarize_by:
            return self._memo(key, lambda: self._summarize(f, summarize_by, page, page_size, sort_dir))
        where, params = f.where("r")
        return self._memo(key, lambda: self._select_rows(where, params, page, page_size, sort_by, sort_dir, f))

    def _summarize(self, f, by, page, page_size, sort_dir):
        if by not in SUMMARIZE:
            raise ValueError(f"summarize_by must be one of {sorted(SUMMARIZE)}")
        where, params = f.where("r")
        keys = [k.split(".")[1] for k in SUMMARIZE[by]]
        g = ", ".join(SUMMARIZE[by])
        names = {"code": ("", ""),
                 "tin_code": (f", t.display_name AS tin_name, {f.tag_expr('tag_name', 't')} AS tag_name",
                              f"LEFT JOIN {S.T_TINS} t ON t.tin = a.tin"),
                 "npi_code": (", p.provider_name, p.provider_type_group",
                              f"LEFT JOIN {S.T_PROVIDERS} p ON p.npi = a.npi")}[by]
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        page, page_size = max(1, int(page)), max(1, min(int(page_size), 5000))
        agg = f"""SELECT {g}, count(*) AS n_rows, count(DISTINCT r.npi) AS n_npis, count(DISTINCT r.tin) AS n_tins,
                         avg(r.negotiated_rate) AS avg_rate, median(r.negotiated_rate) AS median_rate,
                         min(r.negotiated_rate) AS min_rate, max(r.negotiated_rate) AS max_rate
                  FROM {self._tbl(f)} r WHERE {where} GROUP BY {g}"""
        total = self._rows(f"SELECT count(*) AS n FROM ({agg})", params)[0]["n"]
        order = ", ".join(f"a.{k}" for k in keys)
        rows = self._rows(f"""SELECT a.*{names[0]} FROM (
                SELECT * FROM ({agg}) ORDER BY avg_rate {direction}, {", ".join(keys)} LIMIT ? OFFSET ?) a {names[1]}
            ORDER BY a.avg_rate {direction}, {order}""", params + [page_size, (page - 1) * page_size])
        return {"total": total, "page": page, "page_size": page_size, "summarize_by": by, "rows": rows}

    def percentage_rates(self, f: Filters, page=1, page_size=200, sort_by="negotiated_rate", sort_dir="asc"):
        """Rate Explorer for percentage-of-billed-charges rows only, plus billed_charge / estimated_dollars."""
        fp = f.replace(dollar_rates_only=False)
        where, params = fp.where("r")
        where += " AND r.rate_unit = 'percent_of_billed_charges'"
        res = self._memo(("percentage_rates", f, page, page_size, sort_by, sort_dir),
                         lambda: self._select_rows(where, params, page, page_size, sort_by, sort_dir, fp))
        rows = [dict(r, billed_charge=None, estimated_dollars=None) for r in res["rows"]]
        return dict(res, rows=rows)

    def export_csv(self, f: Filters, path, summarize_by=None, percentage=False):
        """Write the current Rate Explorer (or Percentage Rates) selection to a CSV file. Returns the row count."""
        if percentage:
            f = f.replace(dollar_rates_only=False)
        where, params = f.where("r")
        if percentage:
            where += " AND r.rate_unit = 'percent_of_billed_charges'"
        if summarize_by:
            g = ", ".join(SUMMARIZE[summarize_by])
            sql = f"""SELECT {g}, count(*) AS n_rows, count(DISTINCT r.npi) AS n_npis, avg(r.negotiated_rate) AS avg_rate,
                             median(r.negotiated_rate) AS median_rate FROM {self._tbl(f)} r WHERE {where} GROUP BY {g}
                      ORDER BY {g}"""
        else:
            cols = ", ".join(f"{sql} AS {name}" for name, sql in EXPLORER_COLUMNS.items())
            sql = f"""SELECT {cols}, {f.tag_expr()} AS tag_name FROM {self._tbl(f)} r
                      LEFT JOIN {S.T_PROVIDERS} p ON p.npi = r.npi LEFT JOIN {S.T_TINS} t ON t.tin = r.tin
                      LEFT JOIN {S.T_POS_SETS} ps ON ps.pos_set_id = r.pos_set_id
                      WHERE {where} ORDER BY r.rate_id"""
        cur = self._con.cursor()
        try:
            n = cur.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
            cur.execute(f"COPY ({sql}) TO '{str(path).replace(chr(39), chr(39) * 2)}' (HEADER, DELIMITER ',')", params)
        finally:
            cur.close()
        return n

    # ---- 8.5 Map ---------------------------------------------------------------------------------------------
    def map_points(self, f: Filters, code=None, max_points=5000):
        """One point per ZIP centroid: providers, TINs, median rate, market percentile, tags."""
        code = code or (f.codes[0] if f.codes else "90837")
        return self._memo(("map_points", f, code, max_points), lambda: self._map_points(f, code, max_points))

    def _map_points(self, f, code, max_points):
        fc = f.replace(codes=(code,))
        units_sql, uparams = self._units_cte(fc)
        where, params = fc.where("r")
        tag, ttype = f.tag_expr("tag_name", "r"), f.tag_expr("tag_type", "r")
        pts = self._rows(f"""
            WITH market AS ({units_sql}), n AS (SELECT count(*) AS n FROM market),
                 base AS (SELECT r.npi, r.tin, r.negotiated_rate, {ttype} AS tag_type, {tag} AS tag_name
                          FROM {self._tbl(fc)} r WHERE {where}),
                 pts AS (SELECT p.lat, p.lon, any_value(p.practice_zip5) AS zip5, any_value(p.practice_city) AS city,
                                count(DISTINCT b.npi) AS n_providers, count(DISTINCT b.tin) AS n_tins,
                                median(b.negotiated_rate) AS median_rate,
                                bool_or(b.tag_type = 'platform') AS has_platform,
                                bool_or(b.tag_type = 'health_system') AS has_health_system,
                                list(DISTINCT b.tag_name) FILTER (WHERE b.tag_name IS NOT NULL) AS tag_names
                         FROM base b JOIN {S.T_PROVIDERS} p ON p.npi = b.npi WHERE p.lat IS NOT NULL
                         GROUP BY p.lat, p.lon)
            SELECT pts.*, (SELECT count(*) FROM market m WHERE m.v <= pts.median_rate) * 100.0 / nullif(n.n, 0)
                          AS market_percentile
            FROM pts, n ORDER BY n_providers DESC, lat, lon LIMIT ?""", uparams + params + [max_points])
        unmapped = self._rows(f"""SELECT count(DISTINCT r.npi) AS n FROM {self._tbl(fc)} r
            LEFT JOIN {S.T_PROVIDERS} p ON p.npi = r.npi WHERE {where} AND p.lat IS NULL""", params)[0]["n"]
        return {"code": code, "counting_unit": f.counting_unit, "points": pts, "unmapped_providers": unmapped,
                "capped": len(pts) >= max_points}

    def zip_providers(self, f: Filters, lat, lon, code=None, limit=500):
        """Providers behind one map point (for the popup)."""
        code = code or (f.codes[0] if f.codes else "90837")
        where, params = f.replace(codes=(code,)).where("r")
        return self._rows(f"""SELECT r.npi, any_value(p.provider_name) AS provider_name,
                                     any_value(r.provider_type_group) AS provider_type_group,
                                     list(DISTINCT r.tin) AS tins, median(r.negotiated_rate) AS median_rate,
                                     any_value({f.tag_expr()}) AS tag_name
                              FROM {self._tbl(f)} r JOIN {S.T_PROVIDERS} p ON p.npi = r.npi
                              WHERE {where} AND p.lat = ? AND p.lon = ?
                              GROUP BY r.npi ORDER BY median_rate LIMIT ?""", params + [lat, lon, limit])

    # ---- 8.6 Rate Type Composition -------------------------------------------------------------------------------
    def rate_type_composition(self, f: Filters, by_network=True):
        """Counts of negotiated / fee schedule / percentage / derived / per diem by code (and network)."""
        return self._memo(("rate_type_composition", f, by_network), lambda: self._composition(f, by_network))

    def _composition(self, f, by_network):
        where, params = f.where("r", ignore={"dollar_rates_only"})
        net = ", network" if by_network else ""
        return self._rows(f"""SELECT billing_code{net}, negotiated_type, rate_unit, count(DISTINCT tin) AS n_tins,
                                     count(DISTINCT npi) AS n_npis, count(*) AS n_rows
                              FROM {self._source(self._tbl(f, {"dollar_rates_only"}), by_network)} r WHERE {where}
                              GROUP BY ALL ORDER BY billing_code{net}, n_rows DESC""", params)

    # ---- 8.7 Provider Profile -----------------------------------------------------------------------------------
    PROFILE_IGNORE = {"provider_types", "states", "cities", "zips", "search", "tins", "npis", "tag_types", "tag_names"}

    def provider_profile(self, npi, f: Filters):
        """Everything about one NPI: details, TINs, networks, per-code rates with percentile ranks, rate index."""
        return self._memo(("provider_profile", npi, f), lambda: self._profile("npi", npi, f))

    def billing_entity_profile(self, tin, f: Filters):
        """Everything about one TIN: names, tag, NPIs, provider-type mix, per-code rates, uniform vs split."""
        return self._memo(("billing_entity_profile", tin, f), lambda: self._profile("tin", tin, f))

    def _profile(self, kind, key, f):
        col = "r.npi" if kind == "npi" else "r.tin"
        where, params = f.where("r", ignore=self.PROFILE_IGNORE)
        tbl = self._tbl(f, self.PROFILE_IGNORE)
        units_sql, uparams = self._units_cte(f, ignore=self.PROFILE_IGNORE)
        market = self._market(units_sql, uparams)
        per_code = self._rows(f"""SELECT r.billing_code, median(r.negotiated_rate) AS rate,
                                         list(DISTINCT r.negotiated_rate ORDER BY r.negotiated_rate) AS distinct_rates,
                                         count(DISTINCT r.npi) AS n_npis, count(*) AS n_rows
                                  FROM {tbl} r WHERE {where} AND {col} = ? GROUP BY 1 ORDER BY 1""", params + [key])
        for r in per_code:
            below, at, n, med = self._rank(market.get(r["billing_code"], []), r["rate"])
            r.update(market_median=med, n_market=n, pct_below=below, pct_at_or_below=at,
                     rate_ratio=r["rate"] / med if med else None)
        ratios = [r["rate_ratio"] for r in per_code if r["rate_ratio"] is not None]
        out = {"counting_unit": f.counting_unit, "per_code": per_code,
               "rate_index": sum(ratios) / len(ratios) if ratios else None,
               "networks": [r["n"] for r in self._rows(f"""SELECT DISTINCT unnest(networks) AS n FROM {tbl} r
                                                          WHERE {where} AND {col} = ? ORDER BY 1""", params + [key])]}
        if kind == "npi":
            info = self._rows(f"SELECT * FROM {S.T_PROVIDERS} WHERE npi = ?", [key])
            out["provider"] = info[0] if info else None
            out["tins"] = self._rows(f"""SELECT r.tin, any_value(t.display_name) AS tin_name,
                                                any_value({f.tag_expr()}) AS tag_name, count(*) AS n_rows
                                         FROM {tbl} r LEFT JOIN {S.T_TINS} t ON t.tin = r.tin
                                         WHERE {where} AND r.npi = ? GROUP BY r.tin ORDER BY n_rows DESC""",
                                     params + [key])
        else:
            info = self._rows(f"SELECT * EXCLUDE (search_text) FROM {S.T_TINS} WHERE tin = ?", [key])
            out["entity"] = info[0] if info else None
            out["provider_type_mix"] = self._rows(f"""SELECT r.provider_type_group, count(DISTINCT r.npi) AS n_npis
                FROM {tbl} r WHERE {where} AND r.tin = ? GROUP BY 1 ORDER BY 2 DESC""", params + [key])
            uniform = self._rows(f"""SELECT billing_code, max(n) = 1 AS uniform, max(n) AS max_distinct_rates FROM (
                    SELECT r.billing_code, r.networks, r.pos_group, count(DISTINCT r.negotiated_rate) AS n
                    FROM {tbl} r WHERE {where} AND r.tin = ? GROUP BY 1, 2, 3) GROUP BY 1""", params + [key])
            u = {r["billing_code"]: r for r in uniform}
            for r in per_code:
                r["uniform"] = u.get(r["billing_code"], {}).get("uniform")
                r["max_distinct_rates"] = u.get(r["billing_code"], {}).get("max_distinct_rates")
            out["n_npis"] = self._rows(f"SELECT count(DISTINCT r.npi) AS n FROM {tbl} r WHERE {where} AND r.tin = ?",
                                       params + [key])[0]["n"]
        return out

    def provider_list(self, f: Filters, page=1, page_size=100, sort_by="rate_index", sort_dir="desc"):
        """Unique NPIs under the filters with their rate index (average of rate / market median across codes)."""
        return self._memo(("provider_list", f, page, page_size, sort_by, sort_dir),
                          lambda: self._entity_list("npi", f, page, page_size, sort_by, sort_dir))

    def entity_list(self, f: Filters, page=1, page_size=100, sort_by="rate_index", sort_dir="desc"):
        """Unique TINs under the filters with their rate index."""
        return self._memo(("entity_list", f, page, page_size, sort_by, sort_dir),
                          lambda: self._entity_list("tin", f, page, page_size, sort_by, sort_dir))

    def _entity_list(self, kind, f, page, page_size, sort_by, sort_dir):
        sorts = {"rate_index": "rate_index", "n_codes": "n_codes", "name": "name", "n_npis": "n_npis"}
        if sort_by not in sorts:
            raise ValueError(f"sort_by must be one of {sorted(sorts)}")
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        page, page_size = max(1, int(page)), max(1, min(int(page_size), 5000))
        where, params = f.where("r")
        units_sql, uparams = self._units_cte(f)
        if kind == "npi":
            names = (f"p.provider_name AS name, p.provider_type_group, p.practice_city",
                     f"LEFT JOIN {S.T_PROVIDERS} p ON p.npi = agg.id")
        else:
            names = (f"t.display_name AS name, {f.tag_expr('tag_name', 't')} AS tag_name",
                     f"LEFT JOIN {S.T_TINS} t ON t.tin = agg.id")
        agg = f"""
            WITH market AS ({units_sql}), med AS (SELECT billing_code, median(v) AS m FROM market GROUP BY 1),
                 per AS (SELECT r.{kind} AS id, r.billing_code, median(r.negotiated_rate) AS rate,
                                count(DISTINCT r.npi) AS n_npis
                         FROM {self._tbl(f)} r WHERE {where} GROUP BY 1, 2)
            SELECT id, count(*) AS n_codes, max(n_npis) AS n_npis, avg(rate / nullif(med.m, 0)) AS rate_index
            FROM per JOIN med USING (billing_code) GROUP BY id"""
        total = self._rows(f"SELECT count(*) AS n FROM ({agg})", uparams + params)[0]["n"]
        rows = self._rows(f"""SELECT agg.id AS {kind}, {names[0]}, agg.n_codes, agg.n_npis, agg.rate_index
            FROM ({agg}) agg {names[1]}
            ORDER BY {sorts[sort_by]} {direction} NULLS LAST, agg.id LIMIT ? OFFSET ?""",
                          uparams + params + [page_size, (page - 1) * page_size])
        return {"total": total, "page": page, "page_size": page_size, "rows": rows}

    # ---- 8.9 Data Quality ------------------------------------------------------------------------------------------
    def data_quality(self):
        """Precomputed counts, scope-flag breakdown with the top taxonomies, and rows per source file."""
        return self._memo(("data_quality",), lambda: {
            "stats": self._rows(f"SELECT * FROM {S.T_DQ_STATS} ORDER BY metric"),
            "scope_by_code": self._rows(f"SELECT * FROM {S.T_DQ_SCOPE} ORDER BY billing_code, scope_flag"),
            "top_taxonomies": self._rows(f"SELECT * FROM {S.T_DQ_TAXONOMIES} ORDER BY scope_flag, n_npis DESC"),
            "file_rows": self._rows(f"SELECT * FROM {S.T_DQ_FILE_ROWS}"),
        })

    def dq_rows(self, kind, page=1, page_size=200, sort_by="rate_id", sort_dir="asc"):
        """The rows behind a Data Quality count.

        kind: 'zero_rate' | 'invalid_npi' | 'not_in_nppes' | 'expired' | 'conflicts' | 'scope:<flag>' | 'file:<file_id>'.
        """
        conds = {"zero_rate": ("r.is_zero_rate", []), "invalid_npi": ("NOT r.is_valid_npi", []),
                 "not_in_nppes": ("r.scope_flag = 'not_in_nppes' AND r.is_valid_npi", []),
                 "expired": ("r.is_expired", []),
                 "conflicts": (f"r.rate_id IN (SELECT unnest(rate_ids) FROM {S.T_DQ_CONFLICTS})", [])}
        if kind in conds:
            where, params = conds[kind]
        elif kind.startswith("scope:") and kind[6:] in S.SCOPE_FLAGS:
            where, params = "r.scope_flag = ?", [kind[6:]]
        elif kind.startswith("file:"):
            where, params = (f"r.rate_id IN (SELECT rate_id FROM {S.T_RATE_SOURCES} s JOIN {S.T_FILES} f "
                             f"USING (file_idx) WHERE f.file_id = ?)", [kind[5:]])
        else:
            raise ValueError(f"unknown kind {kind!r}; use {DQ_KINDS}, 'scope:<flag>' or 'file:<file_id>'")
        return self._memo(("dq_rows", kind, page, page_size, sort_by, sort_dir),
                          lambda: self._select_rows(where, params, page, page_size, sort_by, sort_dir))

    def dq_conflicts(self, limit=500):
        return self._rows(f"""SELECT c.* EXCLUDE (pos_set_id), ps.service_code FROM {S.T_DQ_CONFLICTS} c
                              LEFT JOIN {S.T_POS_SETS} ps USING (pos_set_id) ORDER BY n_prices DESC, npi LIMIT ?""", [limit])

    # ---- 9. Traceability -------------------------------------------------------------------------------------------
    def source_rows(self, rate_id):
        """Every source of one rate: file, URL, snapshot, schema version and the JSON paths in the raw file."""
        rows = self._rows(f"""SELECT s.*, f.file_id, f.file_name, f.url, f.descriptions, f.network_names,
                                     f.schema_version, f.last_updated_on, f.sha256, f.is_texas_file
                              FROM (SELECT * FROM {S.T_RATE_SOURCES} WHERE rate_id = {int(rate_id)}) s
                              LEFT JOIN {S.T_FILES} f USING (file_idx) ORDER BY f.file_name, s.i, s.j, s.k""")
        for r in rows:
            r["price_path"] = f"in_network[{r['i']}].negotiated_rates[{r['j']}].negotiated_prices[{r['k']}]"
            r["npi_path"] = (f"provider_references[{r['m']}].provider_groups[{r['n']}].npi[{r['p']}]"
                             if r["m"] is not None else None)
            r["snapshot_date"] = self._meta["snapshot_date"]
        return rows

    def capitation(self):
        """Capitation payments (fixed per-member amounts) with their payees and covered codes."""
        return self._memo(("capitation",), lambda: self._rows(f"SELECT * FROM {S.T_CAPITATION} ORDER BY negotiated_rate"))
