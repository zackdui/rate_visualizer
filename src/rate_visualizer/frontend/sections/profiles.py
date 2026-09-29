"""8.7 Provider Profile (NPI) and 8.8 Billing Entity Profile (TIN), each with search, a profile and a sortable list."""
import asyncio
from nicegui import ui
from .. import theme
from ..state import Section
from . import Pager, col, grid, set_rows

PER_CODE = [col("billing_code", "Code", width=85), col("rate", "Rate (median)", "money", 120),
            col("distinct_rates", "Distinct rates", "list", 200), col("market_median", "Market median", "money", 125),
            col("rate_ratio", "Rate ÷ median", "ratio", 120), col("pct_below", "% below", "pct", 95),
            col("pct_at_or_below", "% at or below", "pct", 120), col("n_market", "Market n", "int", 100)]


class _Profile(Section):
    kind = ""            # "provider" | "entity"
    search_label = ""

    def __init__(self, state):
        super().__init__(state)
        self.current = None
        self.list_sort, self.list_dir = "rate_index", "desc"

    def body(self):
        with ui.row().classes("items-start gap-4 w-full no-wrap"):
            with ui.column().classes("w-80 gap-1"):
                box = ui.input(self.search_label, on_change=lambda e: asyncio.create_task(self._search(e.value)))
                box.props("dense outlined clearable debounce=300").classes("w-full")
                self.results = ui.column().classes("gap-0 w-full")
            self.profile = ui.column().classes("grow gap-2 min-w-0")
            with self.profile:
                ui.label("Search above, or click a row in the list below or an NPI/TIN in the Rate Explorer.")\
                    .classes("section-sub")
        ui.separator()
        with ui.row().classes("items-center gap-3 w-full"):
            ui.label(self.list_title).classes("font-medium")
            ui.select({"rate_index": "Rate index", "n_codes": "# codes", "n_npis": "# NPIs", "name": "Name"},
                      value="rate_index", label="Sort",
                      on_change=lambda e: self._sort(e.value)).props("dense outlined options-dense").classes("w-40")
            ui.toggle({"desc": "↓", "asc": "↑"}, value="desc", on_change=lambda e: self._sort(dir_=e.value))\
                .props("dense unelevated toggle-color=primary")
            self.pager = Pager(self._refresh_list)
            self.pager.page_size = 100
        self.list = grid(self.list_columns, height="h-80")
        self.list.on("cellClicked", lambda e: self.open((e.args.get("data") or {}).get(self.id_field)))

    async def _search(self, text):
        self.results.clear()
        if not text or len(text.strip()) < 2:
            return
        hits = await self.state.call(self.state.backend.suggest, self.kind, text, 12)
        with self.results:
            if not hits:
                ui.label("No matches").classes("section-sub")
            for h in hits:
                ui.button(f"{h['label'] or h['value']}  ·  {h['value']}", on_click=lambda _, v=h["value"]: self.open(v))\
                    .props("flat dense no-caps align=left").classes("w-full text-left")

    async def _sort(self, by=None, dir_=None):
        self.list_sort = by or self.list_sort
        self.list_dir = dir_ or self.list_dir
        self.pager.page = 1
        await self._refresh_list()

    async def open(self, key):
        if not key:
            return
        self.current = key
        await self._draw_profile()

    async def refresh(self):
        await asyncio.gather(self._draw_profile(), self._refresh_list())

    async def _refresh_list(self):
        fn = self.state.backend.provider_list if self.kind == "provider" else self.state.backend.entity_list
        res = await self.state.call(fn, self.state.filters, self.pager.page, self.pager.page_size, self.list_sort,
                                    self.list_dir)
        set_rows(self.list, res["rows"])
        self.pager.show(res["total"], res["page"], res["page_size"])


class ProviderProfile(_Profile):
    key, anchor, title = "provider", "provider-profile", "Provider Profile"
    subtitle = "One NPI: every TIN it bills under, networks, rate and percentile per code, and its rate index."
    kind, search_label, id_field, list_title = "provider", "Search NPI or provider name", "npi", "All providers (NPIs)"
    list_columns = [col("npi", "NPI", width=115), col("name", "Provider", width=220),
                    col("provider_type_group", "Type", width=120), col("practice_city", "City", width=130),
                    col("n_codes", "# codes", "int", 95), col("rate_index", "Rate index", "ratio", 115)]

    def __init__(self, state):
        super().__init__(state)
        state.provider = self

    async def _draw_profile(self):
        if not self.current:
            return
        p = await self.state.call(self.state.backend.provider_profile, self.current, self.state.filters)
        info = p.get("provider") or {}
        self.profile.clear()
        with self.profile:
            with ui.row().classes("items-baseline gap-3"):
                ui.label(info.get("provider_name") or f"NPI {self.current}").classes("text-lg font-semibold")
                ui.label(f"NPI {self.current} · {info.get('entity_type') or ''} · {info.get('provider_type_group') or ''}"
                         f" · {info.get('specialty_display_name') or info.get('specialty_classification') or ''}")\
                    .classes("section-sub")
            ui.label(f"{info.get('practice_address_1') or ''}, {info.get('practice_city') or ''} "
                     f"{info.get('practice_state') or ''} {info.get('practice_zip5') or ''}").classes("text-sm")
            with ui.row().classes("gap-6"):
                _stat("Rate index", theme.ratio(p["rate_index"]))
                _stat("Codes with rates", str(len(p["per_code"])))
                _stat("Networks", ", ".join(p["networks"]) or "—")
            with ui.row().classes("gap-2 flex-wrap"):
                ui.label("Bills under:").classes("text-sm")
                for t in p["tins"]:
                    ui.button(f"{t['tin_name'] or t['tin']} ({t['tin']})" + (f" · {t['tag_name']}" if t["tag_name"] else ""),
                              on_click=lambda _, v=t["tin"]: self.state.open_entity(v))\
                        .props("outline dense no-caps size=sm")
            g = grid(PER_CODE, height="h-64")
            set_rows(g, p["per_code"])
            g.on("cellClicked", lambda e: self.state.drill(npis=(self.current,),
                                                           codes=((e.args.get("data") or {}).get("billing_code"),)))


class BillingEntityProfile(_Profile):
    key, anchor, title = "entity", "billing-entity-profile", "Billing Entity Profile"
    subtitle = "One TIN: names, tag, NPIs, provider-type mix, rates per code, and whether all its NPIs get the same rate."
    kind, search_label, id_field, list_title = "entity", "Search TIN or business name", "tin", "All billing entities (TINs)"
    list_columns = [col("tin", "TIN", width=115), col("name", "Business name", width=260), col("tag_name", "Tag", width=150),
                    col("n_npis", "# NPIs", "int", 95), col("n_codes", "# codes", "int", 95),
                    col("rate_index", "Rate index", "ratio", 115)]

    def __init__(self, state):
        super().__init__(state)
        state.entity = self

    async def _draw_profile(self):
        if not self.current:
            return
        p = await self.state.call(self.state.backend.billing_entity_profile, self.current, self.state.filters)
        e = p.get("entity") or {}
        self.profile.clear()
        with self.profile:
            with ui.row().classes("items-baseline gap-3"):
                ui.label(e.get("display_name") or f"TIN {self.current}").classes("text-lg font-semibold")
                ui.label(f"TIN {self.current} ({e.get('tin_type') or ''})").classes("section-sub")
                if e.get("tag_name"):
                    ui.badge(f"{e['tag_name']} · {e.get('tag_confidence')}",
                             color="purple-2" if e.get("tag_type") == "platform" else "orange-2", text_color="grey-10")
            variants = [v for v in (e.get("name_variants") or []) if v != e.get("display_name")][:6]
            if variants:
                ui.label("Also listed as: " + "; ".join(variants)).classes("text-xs section-sub")
            with ui.row().classes("gap-6"):
                _stat("NPIs (under filters)", theme.num(p["n_npis"]))
                _stat("Rate index", theme.ratio(p["rate_index"]))
                _stat("Networks", ", ".join(p["networks"]) or "—")
            mix = ", ".join(f"{m['provider_type_group']} {m['n_npis']:,}" for m in p["provider_type_mix"])
            ui.label(f"Provider-type mix: {mix or '—'}").classes("text-sm")
            rows = [dict(r, uniform_label="Uniform" if r.get("uniform") else
                         (f"Split ({r['max_distinct_rates']} rates)" if r.get("uniform") is False else "—"))
                    for r in p["per_code"]]
            g = grid(PER_CODE + [col("uniform_label", "Uniform vs split", width=150), col("n_npis", "# NPIs", "int", 90)],
                     height="h-64")
            set_rows(g, rows)
            g.on("cellClicked", lambda ev: self.state.drill(tins=(self.current,),
                                                            codes=((ev.args.get("data") or {}).get("billing_code"),)))


def _stat(label, value):
    with ui.column().classes("gap-0"):
        ui.label(label).classes("stat-label")
        ui.label(value).classes("stat-value")
