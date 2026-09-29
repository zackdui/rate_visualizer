"""8.9 Data Quality: precomputed counts (each opens its rows), scope flags, top taxonomies, rows per file."""
from nicegui import ui
from ...backend import schema as S
from .. import theme
from ..state import Section
from . import Pager, col, grid, set_rows

TILES = [  # (dq_stats metric, label, dq_rows kind or None)
    ("rows_removed_by_dedup", "Rows removed by deduplication", None),
    ("zero_rate_rows", "$0 rates", "zero_rate"),
    ("conflicting_keys", "Keys with conflicting prices", "conflicts"),
    ("npis_not_in_nppes", "NPIs not in NPPES", "not_in_nppes"),
    ("scope_ghost_candidate_rows", "Ghost-rate candidate rows", "scope:ghost_candidate"),
    ("scope_non_bh_clinician_rows", "Non-BH clinician rows", "scope:non_bh_clinician"),
    ("invalid_npi_rows", "Rows with NPI = 0 / invalid", "invalid_npi"),
    ("expired_rows", "Expired rows", "expired"),
]
ROW_COLS = [col("billing_code", "Code", width=85), col("negotiated_rate", "Rate", "money", 100),
            col("negotiated_type", "Type", width=110), col("npi", "NPI", width=115), col("provider_name", "Provider", width=200),
            col("provider_type_group", "Provider type", width=125), col("specialty", "Specialty", width=180),
            col("tin", "TIN", width=115), col("tin_name", "Business name", width=200), col("scope_flag", "Scope flag", width=140),
            col("networks", "Networks", "list", 200), col("expiration_date", "Expires", width=105)]


class DataQuality(Section):
    key, anchor, title = "quality", "data-quality", "Data Quality"
    subtitle = "What was removed, flagged or looks suspicious in this snapshot. Click a count to see its rows."
    shows_unit = False

    def body(self):
        self.tiles = ui.grid(columns="repeat(auto-fill, minmax(210px, 1fr))").classes("w-full gap-3")
        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            with ui.column().classes("grow min-w-0"):
                ui.label("Scope flags by code (professional dollar rates)").classes("font-medium")
                self.scope = grid([col("billing_code", "Code", width=85), col("scope_label", "Scope flag", width=320),
                                   col("n_npis", "# NPIs", "int", 100), col("n_rows", "# rows", "int", 110)], height="h-72")
            with ui.column().classes("grow min-w-0"):
                ui.label("Top specialties behind non-BH / ghost flags").classes("font-medium")
                self.tax = grid([col("scope_flag", "Flag", width=150), col("specialty", "Specialty", width=240),
                                 col("n_npis", "# NPIs", "int", 100)], height="h-72")
        ui.label("Rows per source file (before deduplication); click a file to see its rows").classes("font-medium")
        self.files = grid([col("file_name", "File", width=520), col("schema_version", "Schema", width=190),
                           col("rows_before_dedup", "Rows", "int", 120)], height="h-64")
        self.files.on("cellClicked", lambda e: self._open(f"file:{(e.args.get('data') or {}).get('file_id')}",
                                                          (e.args.get("data") or {}).get("file_name")))

    async def refresh(self):
        dq = await self.state.call(self.state.backend.data_quality)
        stats = {s["metric"]: s for s in dq["stats"]}
        self.tiles.clear()
        with self.tiles:
            for metric, label, kind in TILES:
                v = stats.get(metric, {}).get("value")
                card = ui.card().classes("section-card p-3 gap-0" + (" cursor-pointer" if kind else ""))
                with card:
                    ui.label(label).classes("stat-label")
                    ui.label(theme.num(v)).classes("stat-value")
                    note = stats.get(metric, {}).get("note")
                    if note:
                        ui.label(note).classes("text-xs section-sub")
                if kind:
                    card.on("click", lambda _, k=kind, l=label: self._open(k, l))
        set_rows(self.scope, [dict(r, scope_label=S.SCOPE_LABELS.get(r["scope_flag"], r["scope_flag"]))
                              for r in dq["scope_by_code"]])
        set_rows(self.tax, dq["top_taxonomies"])
        set_rows(self.files, dq["file_rows"])

    async def _open(self, kind, label):
        with ui.dialog() as d, ui.card().classes("w-[1100px] max-w-[95vw]"):
            ui.label(label).classes("font-semibold")
            g = grid(ROW_COLS, height="h-[480px]")

            async def load():
                res = await self.state.call(self.state.backend.dq_rows, kind, pager.page, 200)
                set_rows(g, res["rows"])
                pager.show(res["total"], res["page"], res["page_size"])
            pager = Pager(load)
            ui.button("Close", on_click=d.close).props("flat")
        d.open()
        await load()
