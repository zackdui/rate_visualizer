"""8.6 Rate Type Composition: negotiated / fee schedule / percentage / derived / per diem by code (and network)."""
import asyncio
from collections import defaultdict
from nicegui import ui
from .. import theme
from ..state import Section
from . import col, grid, set_rows

TYPE_COLORS = {"negotiated": theme.ACCENT, "fee schedule": "#0891B2", "percentage": "#F59E0B", "derived": "#7C3AED",
               "per diem": "#DB2777"}


class RateTypeComposition(Section):
    key, anchor, title = "composition", "rate-type-composition", "Rate Type Composition"
    subtitle = "How rates are expressed, by code and network (ignores the 'Dollar rates only' toggle)."
    shows_unit = False

    def body(self):
        with ui.row().classes("items-center gap-3"):
            self.measure = ui.toggle({"n_tins": "# TINs", "n_npis": "# NPIs", "n_rows": "# rows"}, value="n_tins",
                                     on_change=lambda: asyncio.create_task(self.safe_refresh()))
            self.measure.props("dense no-caps unelevated toggle-color=primary")
            self.by_network = ui.switch("Split by network", value=True,
                                        on_change=lambda: asyncio.create_task(self.safe_refresh())).props("dense")
        self.chart = ui.echart({"series": []}).classes("w-full h-72")
        self.grid = grid([col("billing_code", "Code", width=90), col("network", "Network", width=190),
                          col("negotiated_type", "Type", width=130), col("rate_unit", "Unit", width=170),
                          col("n_tins", "# TINs", "int", 100), col("n_npis", "# NPIs", "int", 100),
                          col("n_rows", "# rows", "int", 110)], height="h-72")

    async def refresh(self):
        rows = await self.state.call(self.state.backend.rate_type_composition, self.state.filters, self.by_network.value)
        set_rows(self.grid, rows)
        m = self.measure.value
        totals = defaultdict(lambda: defaultdict(int))
        for r in rows:
            totals[r["negotiated_type"]][r["billing_code"]] += r[m]
        codes = sorted({r["billing_code"] for r in rows})
        self.chart.options.clear()
        self.chart.options.update({
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"top": 0},
            "grid": {"left": 60, "right": 16, "top": 36, "bottom": 28},
            "xAxis": {"type": "category", "data": codes},
            "yAxis": {"type": "value", "splitLine": {"lineStyle": {"opacity": .3}}},
            "series": [{"name": t, "type": "bar", "stack": "all", "data": [totals[t].get(c, 0) for c in codes],
                        "itemStyle": {"color": TYPE_COLORS.get(t, theme.MUTED)}} for t in sorted(totals)],
        })
        self.chart.update()
