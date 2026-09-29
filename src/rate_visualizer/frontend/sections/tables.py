"""8.3 Rate Explorer and 8.4 Percentage Rates (server-side sorting, paging, grouping; CSV export; source links)."""
import asyncio, os, tempfile
from nicegui import ui
from ...backend import schema as S
from ...backend.queries import EXPLORER_COLUMNS, SUMMARIZE_LABELS
from .. import theme
from ..state import Section
from . import Pager, col, grid, set_rows

ROW_COLUMNS = [
    col("billing_code", "Code", width=85, pinned="left"), col("negotiated_rate", "Rate", "money", 100, pinned="left"),
    col("negotiated_type", "Type", width=115), col("rate_unit", "Unit", width=120), col("networks", "Networks", "list", 220),
    col("service_code", "Place of service", "list", 170), col("pos_group", "Setting", width=120),
    col("billing_code_modifier", "Modifier", "list", 95), col("billing_class", "Class", width=110),
    col("expiration_date", "Expires", width=110), col("npi", "NPI", width=115, cellClass="text-primary cursor-pointer"),
    col("provider_name", "Provider", width=200, cellClass="text-primary cursor-pointer"),
    col("provider_type_group", "Provider type", width=125), col("specialty", "Specialty", width=180),
    col("tin", "TIN", width=115, cellClass="text-primary cursor-pointer"),
    col("tin_name", "Business name", width=220, cellClass="text-primary cursor-pointer"),
    col("tag_name", "Tag", width=140), col("practice_city", "City", width=130), col("practice_state", "State", width=80),
    col("practice_zip5", "ZIP", width=85), col("scope_flag", "Scope flag", width=140), col("n_sources", "Sources", "int", 90),
    col("source", "Source", width=90, sortable=False, filter=False, pinned="right", cellClass="text-primary cursor-pointer"),
]
GROUP_COLUMNS = {  # output column -> header
    "billing_code": "Code", "tin": "TIN", "tin_name": "Business name", "npi": "NPI", "provider_name": "Provider",
    "provider_type_group": "Provider type", "network": "Network", "pos_group": "Setting", "tag_name": "Tag",
}
GROUP_STATS = [col("n_rows", "Rows", "int", 90), col("n_npis", "# NPIs", "int", 90), col("n_tins", "# TINs", "int", 90),
               col("avg_rate", "Average", "money", 105), col("median_rate", "Median", "money", 105),
               col("min_rate", "Min", "money", 95), col("max_rate", "Max", "money", 95)]
DRILL_TO_FILTER = {"billing_code": "codes", "tin": "tins", "npi": "npis", "network": "networks",
                   "provider_type_group": "provider_types", "pos_group": "pos_groups", "tag_name": "tag_names"}
SORT_LABELS = {k: next((c["headerName"] for c in ROW_COLUMNS if c["field"] == k), k) for k in EXPLORER_COLUMNS}


class RateExplorer(Section):
    key, anchor, title = "explorer", "rate-explorer", "Rate Explorer"
    subtitle = ("Every rate row under the filters (dollar rates only when that toggle is on). Group or summarise over "
                "all matching rows; click a group to open its rows, an NPI/TIN to open its profile, or 'view' for the "
                "source.")
    percentage = False

    def __init__(self, state):
        super().__init__(state)
        self.drill = {}
        self.sort_by, self.sort_dir, self.group = "negotiated_rate", "asc", None
        if not self.percentage:
            state.explorer = self

    def body(self):
        with ui.row().classes("items-center gap-3 w-full flex-wrap"):
            if not self.percentage:
                groups = {"none": "No grouping (rows)", **SUMMARIZE_LABELS}
                self.group_sel = ui.select(groups, value="none", label="Summarize / group by",
                                           on_change=lambda e: self._set(group=None if e.value == "none" else e.value))
                self.group_sel.props("dense outlined options-dense").classes("w-64")
            self.sort_sel = ui.select(SORT_LABELS, value=self.sort_by, label="Sort by",
                                      on_change=lambda e: self._set(sort_by=e.value))
            self.sort_sel.props("dense outlined options-dense").classes("w-48")
            ui.toggle({"asc": "↑", "desc": "↓"}, value="asc", on_change=lambda e: self._set(sort_dir=e.value)) \
                .props("dense unelevated toggle-color=primary")
            self.size_sel = ui.select([50, 200, 500, 1000], value=200, label="Rows",
                                      on_change=lambda e: self._set(page_size=e.value))
            self.size_sel.props("dense outlined options-dense").classes("w-24")
            ui.button("CSV", icon="download", on_click=self._export).props("outline dense no-caps")
            self.pager = Pager(self.safe_refresh)
        self.drill_row = ui.row().classes("items-center gap-2")
        self.grid = grid(ROW_COLUMNS, height="h-[520px]")
        self.grid.on("cellClicked", self._clicked)
        if self.percentage:
            ui.label("billed_charge is empty until providers' own billed-charge data is added; estimated_dollars = "
                     "rate% × billed_charge then fills in automatically.").classes("section-sub")

    def filters(self):
        return self.state.filters.replace(**self.drill)

    async def _set(self, **kw):
        page_size = kw.pop("page_size", None)
        for k, v in kw.items():
            setattr(self, k, v)
        if page_size:
            self.pager.page_size = page_size
        self.pager.page = 1
        await self.safe_refresh()

    async def open_drill(self, overrides):
        self.drill = {k: v for k, v in overrides.items() if v}
        self.group = None
        if not self.percentage:
            self.group_sel.set_value("none")
        self.pager.page = 1
        await self.safe_refresh()

    def _draw_drill(self):
        self.drill_row.clear()
        if not self.drill:
            return
        with self.drill_row:
            ui.icon("subdirectory_arrow_right").classes("text-primary")
            for k, v in self.drill.items():
                ui.badge(f"{k.replace('_', ' ')}: {', '.join(map(str, v))}", color="teal-1", text_color="teal-10")
            ui.button("Clear drill-down", on_click=lambda: self.open_drill({})).props("flat dense no-caps")

    async def refresh(self):
        self._draw_drill()
        b, f = self.state.backend, self.filters()
        if self.percentage:
            res = await self.state.call(b.percentage_rates, f, self.pager.page, self.pager.page_size,
                                        self.sort_by, self.sort_dir)
            columns = ROW_COLUMNS[:2] + [col("billed_charge", "billed_charge", "money", 120),
                                         col("estimated_dollars", "estimated_dollars", "money", 140)] + ROW_COLUMNS[2:]
            columns[1] = dict(columns[1], headerName="Rate (% of billed)", **{":valueFormatter": "p => p.value == null ? '' : p.value + '%'"})
        elif self.group:
            res = await self.state.call(b.rate_explorer, f, self.pager.page, self.pager.page_size, self.sort_by,
                                        self.sort_dir, self.group)
            extra = [k for k in ("tin_name", "provider_name") if res["rows"] and k in res["rows"][0]]
            columns = [col(k, GROUP_COLUMNS.get(k, k), width=150, cellClass="text-primary cursor-pointer")
                       for k in res["keys"] + extra] + GROUP_STATS
        else:
            res = await self.state.call(b.rate_explorer, f, self.pager.page, self.pager.page_size, self.sort_by,
                                        self.sort_dir)
            columns = ROW_COLUMNS
        rows = [dict(r, source="view", pos_group=S.POS_GROUP_LABELS.get(r.get("pos_group"), r.get("pos_group")),
                     _pos_group=r.get("pos_group")) for r in res["rows"]]
        set_rows(self.grid, rows, columns)
        self.pager.show(res["total"], res["page"], res["page_size"])

    async def _clicked(self, e):
        r, field = e.args.get("data") or {}, e.args.get("colId")
        if self.group and not self.percentage:
            drill = {DRILL_TO_FILTER[k]: (r["_pos_group"] if k == "pos_group" else r[k],)
                     for k in DRILL_TO_FILTER if k in r and r[k] is not None}
            await self.open_drill({**self.drill, **drill})
            return
        if field == "source" and r.get("rate_id"):
            await self._show_source(r)
        elif field in ("npi", "provider_name") and r.get("npi"):
            await self.state.open_provider(r["npi"])
        elif field in ("tin", "tin_name") and r.get("tin"):
            await self.state.open_entity(r["tin"])

    async def _show_source(self, r):
        rows = await self.state.call(self.state.backend.source_rows, r["rate_id"])
        with ui.dialog() as d, ui.card().classes("min-w-[640px] max-w-[900px]"):
            ui.label(f"Where this number comes from: {r['billing_code']} {theme.money(r['negotiated_rate'])} "
                     f"for NPI {r['npi']} / TIN {r['tin']}").classes("font-semibold")
            ui.label(f"{len(rows)} source(s). Each position is a path in the raw BCBSTX JSON file; re-check it with "
                     f"`rate-visualizer trace --npi {r['npi']}`.").classes("section-sub")
            for s in rows:
                with ui.card().classes("section-card w-full p-3 gap-1"):
                    ui.label(s["file_name"]).classes("font-medium break-all")
                    ui.label(f"Networks: {', '.join(s['network_names'] or [])} · schema {s['schema_version']} · "
                             f"file updated {s['last_updated_on']} · snapshot {s['snapshot_date']}").classes("text-sm")
                    ui.label(f"Price: {s['price_path']}").classes("text-xs font-mono")
                    if s["npi_path"]:
                        ui.label(f"NPI: {s['npi_path']}").classes("text-xs font-mono")
                    ui.link("Open file URL", s["url"], new_tab=True).classes("text-sm")
            ui.button("Close", on_click=d.close).props("flat")
        d.open()

    async def _export(self):
        path = os.path.join(tempfile.mkdtemp(prefix="rates_export_"), "rates.csv")
        n = await self.state.call(self.state.backend.export_csv, self.filters(), path,
                                  summarize_by=None if self.percentage else self.group, percentage=self.percentage)
        ui.notify(f"Exported {n:,} rows")
        ui.download.file(path, "percentage_rates.csv" if self.percentage else "rates.csv")


class PercentageRates(RateExplorer):
    key, anchor, title = "percentage", "percentage-rates", "Percentage Rates"
    subtitle = "The same columns as the Rate Explorer, for rates paid as a percentage of the provider's billed charge."
    percentage = True
