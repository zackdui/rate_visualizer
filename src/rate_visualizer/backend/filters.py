"""Filters: every global filter and toggle from rates_tool_plan.md section 6, as one immutable object.

The frontend builds a Filters and passes it to Backend methods. It never writes SQL. Filters.where() turns it into
parameterised SQL against the `rates` table (alias `r`), so user text is never pasted into a query.
"""
from dataclasses import asdict, dataclass, fields, replace
from . import schema as S

COUNTING_UNITS = ("tin", "npi", "row")


@dataclass(frozen=True)
class Filters:
    # multi-selects (empty = no restriction)
    codes: tuple = ()
    provider_types: tuple = ()        # schema.PROVIDER_TYPES
    networks: tuple = ()              # e.g. "Blue Choice PPO"
    states: tuple = ()                # NPPES practice state
    cities: tuple = ()                # NPPES practice city (upper case, as published)
    zips: tuple = ()                  # 5-digit practice ZIP
    places_of_service: tuple = ()     # POS codes, e.g. "11"
    pos_groups: tuple = ()            # schema.POS_GROUPS
    billing_classes: tuple = ("professional",)
    tag_types: tuple = ()             # "platform", "health_system", "none"
    tag_names: tuple = ()             # e.g. "Headway"
    min_tag_confidence: str = "high"  # a TIN counts as tagged at this confidence or above
    # contains-match text search on NPI, provider name, TIN or billing-entity names
    search: str = ""
    # drill-down (exact)
    tins: tuple = ()
    npis: tuple = ()
    # toggles (defaults from the product plan)
    exclude_zero: bool = True
    exclude_invalid_npi: bool = True
    texas_files_only: bool = True
    dollar_rates_only: bool = True
    exclude_expired: bool = True
    exclude_platforms_from_market: bool = False
    bh_providers_only: bool = True
    counting_unit: str = "tin"        # "tin" | "npi" | "row"

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if f.type in (tuple, "tuple") and isinstance(v, (list, set)):
                object.__setattr__(self, f.name, tuple(sorted(str(x) for x in v)))
            elif f.type in (tuple, "tuple") and isinstance(v, str):
                object.__setattr__(self, f.name, (v,))
        if self.counting_unit not in COUNTING_UNITS:
            raise ValueError(f"counting_unit must be one of {COUNTING_UNITS}")
        if self.min_tag_confidence not in S.CONFIDENCE_ORDER:
            raise ValueError(f"min_tag_confidence must be one of {tuple(S.CONFIDENCE_ORDER)}")
        bad = set(self.tag_types) - set(S.TAG_TYPES) - {"none"}
        if bad:
            raise ValueError(f"unknown tag_types {sorted(bad)}")

    # ---- convenience for the frontend ----------------------------------------------------------------
    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def to_dict(self):
        return asdict(self)

    def replace(self, **changes):
        return replace(self, **changes)

    # ---- SQL ------------------------------------------------------------------------------------------
    def allowed_confidences(self):
        floor = S.CONFIDENCE_ORDER[self.min_tag_confidence]
        return tuple(c for c, rank in S.CONFIDENCE_ORDER.items() if rank >= floor)

    def tag_expr(self, col="tag_name", alias="r"):
        """The row's tag column, but NULL when the tag is below min_tag_confidence."""
        return f"(CASE WHEN {alias}.tag_confidence IN {_lit(self.allowed_confidences())} THEN {alias}.{col} END)"

    def where(self, alias="r", *, market=False, ignore=()):
        """Return (sql, params) for a WHERE clause over `rates`.

        market=True also applies exclude_platforms_from_market (for market statistics).
        ignore: names of filters to leave out (e.g. {"dollar_rates_only"} for rate-type composition).
        """
        a, parts, params = alias, [], []

        def multi(name, sql):
            vals = getattr(self, name)
            if vals and name not in ignore:
                parts.append(sql.format(a=a, ph=", ".join("?" * len(vals))))
                params.extend(vals)

        multi("codes", "{a}.billing_code IN ({ph})")
        multi("provider_types", "{a}.provider_type_group IN ({ph})")
        multi("networks", "list_has_any({a}.networks, [{ph}])")
        multi("states", "{a}.practice_state IN ({ph})")
        multi("cities", "{a}.practice_city IN ({ph})")
        multi("zips", "{a}.practice_zip5 IN ({ph})")
        multi("places_of_service", "{a}.pos_set_id IN (SELECT pos_set_id FROM " + S.T_POS_SETS +
              " WHERE list_has_any(service_code, [{ph}]))")
        multi("pos_groups", "{a}.pos_group IN ({ph})")
        multi("billing_classes", "{a}.billing_class IN ({ph})")
        multi("tins", "{a}.tin IN ({ph})")
        multi("npis", "{a}.npi IN ({ph})")
        tag = self.tag_expr("tag_name", a)
        ttype = self.tag_expr("tag_type", a)
        if self.tag_types and "tag_types" not in ignore:
            ors = []
            real = [t for t in self.tag_types if t != "none"]
            if real:
                ors.append(f"{ttype} IN ({', '.join('?' * len(real))})")
                params.extend(real)
            if "none" in self.tag_types:
                ors.append(f"{ttype} IS NULL")
            parts.append("(" + " OR ".join(ors) + ")")
        multi("tag_names", tag + " IN ({ph})")
        if self.search.strip() and "search" not in ignore:
            like = f"%{self.search.strip().upper()}%"
            parts.append(f"""({a}.npi LIKE ? OR {a}.tin LIKE ?
                OR {a}.npi IN (SELECT npi FROM {S.T_PROVIDERS} WHERE upper(provider_name) LIKE ?)
                OR {a}.tin IN (SELECT tin FROM {S.T_TINS} WHERE search_text LIKE ?))""")
            params.extend([like, like, like, like])
        toggles = {
            "exclude_zero": f"NOT {a}.is_zero_rate",
            "exclude_invalid_npi": f"{a}.is_valid_npi",
            "texas_files_only": f"{a}.is_texas_file",
            "dollar_rates_only": f"({a}.rate_unit = 'dollars' AND {a}.negotiated_type IN {_lit(S.DOLLAR_TYPES)})",
            "exclude_expired": f"NOT {a}.is_expired",
            "bh_providers_only": f"{a}.scope_flag = 'expected'",
        }
        for name, sql in toggles.items():
            if getattr(self, name) and name not in ignore:
                parts.append(sql)
        if market and self.exclude_platforms_from_market and "exclude_platforms_from_market" not in ignore:
            parts.append(f"{ttype} IS DISTINCT FROM 'platform'")
        return (" AND ".join(parts) if parts else "TRUE"), params

    # rates_core = rows allowed by these toggles + professional class (dollar_rates_only is applied on top of it)
    CORE_TOGGLES = ("exclude_zero", "exclude_invalid_npi", "texas_files_only", "exclude_expired", "bh_providers_only")

    def fits_core(self, ignore=()):
        """True if the rows this filter keeps (with `ignore` left out) are all inside rates_core, which holds exactly
        the rows allowed by the default toggles except dollar_rates_only, and the professional class."""
        if any(not getattr(self, t) or t in ignore for t in self.CORE_TOGGLES):
            return False
        return ("billing_classes" not in ignore and bool(self.billing_classes)
                and set(self.billing_classes) <= {"professional"})

    def unit_key(self, alias="r"):
        """The column that defines one counted unit."""
        return {"tin": f"{alias}.tin", "npi": f"{alias}.npi", "row": f"{alias}.rate_id"}[self.counting_unit]


def _lit(values):
    return "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"
