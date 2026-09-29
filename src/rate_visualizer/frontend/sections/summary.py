"""8.1 Code Summary and 8.2 Benchmarks."""
import asyncio
from nicegui import ui
from ...backend import schema as S
from .. import theme
from ..state import Section
from . import col, grid, set_rows

BREAKDOWNS = {"none": "By code", "provider_type": "× provider type", "network": "× network",
              "place_of_service": "× setting"}
BREAKDOWN_FILTER = {"provider_type": "provider_types", "network": "networks", "place_of_service": "pos_groups"}


class CodeSummary(Section):
    key, anchor, title = "code_summary", "code-summary", "Code Summary"
    subtitle = ("Counts and percentiles per code under the current filters. Click a row or a chart to open its rates "
                "in the Rate Explorer.")

    def body(self):
        with ui.row().classes("items-center gap-3"):
            self.breakdown = ui.toggle(BREAKDOWNS, value="none", on_change=lambda: asyncio.create_task(self.safe_refresh()))
            self.breakdown.props("dense no-caps unelevated toggle-color=primary")
        self.columns_base = [
            col("billing_code", "Code", width=90, pinned="left"), col("label", "Description", width=250),
            col("n_tins", "# TINs", "int", 100), col("n_npis", "# NPIs", "int", 100),
            col("min", "Min", "money", 95), col("p10", "P10", "money", 95), col("p25", "P25", "money", 95),
            col("p50", "Median", "money", 100), col("p75", "P75", "money", 95), col("p90", "P90", "money", 95),
            col("max", "Max", "money", 95), col("mean", "Mean", "money", 95),
            col("pct_percentage_rows", "% on percentage rates", "pct", 150)]
        self.grid = grid(self.columns_base, height="h-72")
        self.grid.on("cellClicked", self._clicked)
        with ui.row().classes("w-full items-center justify-between mt-2"):
            ui.label("Distribution per code").classes("font-medium")
            self.legend = ui.row().classes("gap-3 text-xs")
        self.charts = ui.grid(columns="repeat(auto-fill, minmax(320px, 1fr))").classes("w-full gap-3")

    async def refresh(self):
        f, b = self.state.filters, self.state.backend
        by = None if self.breakdown.value == "none" else self.breakdown.value
        res = await self.state.call(b.code_summary, f, by)
        columns = list(self.columns_base)
        if by:
            columns.insert(1, col("breakdown", BREAKDOWNS[by].replace("× ", "").capitalize(), width=180, pinned="left"))
        rows = []
        for r in res["rows"]:
            r = dict(r)
            if by == "place_of_service":
                r["_pos_group"] = r["breakdown"]
                r["breakdown"] = S.POS_GROUP_LABELS.get(r["breakdown"], r["breakdown"])
            rows.append(r)
        set_rows(self.grid, rows, columns)
        codes = sorted({r["billing_code"] for r in res["rows"]})
        hists = await asyncio.gather(*(self.state.call(b.histogram, f, c) for c in codes))
        self._draw_charts(hists)

    def _draw_charts(self, hists):
        pinned = set(self.state.pinned)
        self.charts.clear()
        self.legend.clear()
        with self.legend:
            for name in sorted(pinned):
                ui.html(f'<span style="color:{marker_color(name, self.state)}">▍</span> {name}')
        with self.charts:
            for h in hists:
                markers = [m for m in h["markers"] if m["tag_name"] in pinned and m["median_rate"] is not None]
                if not h["edges"]:
                    with ui.card().classes("section-card p-3"):
                        ui.label(f"{h['code']}: no rates under these filters").classes("section-sub")
                    continue
                lo = min([h["edges"][0]] + [m["median_rate"] for m in markers])
                hi = max([h["edges"][-1]] + [m["median_rate"] for m in markers])
                centers = [(a + b) / 2 for a, b in zip(h["edges"], h["edges"][1:])]
                opts = {
                    "title": {"text": f"{h['code']}  ·  {S.CODE_LABELS.get(h['code'], '')}",
                              "subtext": f"n = {h['n']:,} ({theme.UNIT_LABEL[h['counting_unit']]})",
                              "textStyle": {"fontSize": 12, "fontWeight": 600, "color": "#64748B"},
                              "subtextStyle": {"fontSize": 11, "color": "#94A3B8"}},
                    "grid": {"left": 44, "right": 14, "top": 52, "bottom": 28},
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "value", "min": lo, "max": hi,
                              "axisLabel": {":formatter": "v => '$' + Math.round(v)", "fontSize": 10}},
                    "yAxis": {"type": "value", "axisLabel": {"fontSize": 10}, "splitLine": {"lineStyle": {"opacity": .3}}},
                    "series": [{"type": "bar", "barWidth": "95%", "data": [[c, n] for c, n in zip(centers, h["counts"])],
                                "itemStyle": {"color": theme.ACCENT, "opacity": .85},
                                "markLine": {"symbol": "none", "silent": True, "data": [
                                    {"xAxis": m["median_rate"], "name": m["tag_name"],
                                     "lineStyle": {"color": marker_color(m["tag_name"], self.state), "width": 2},
                                     "label": {"formatter": f"{m['tag_name']} ${m['median_rate']:.0f}", "fontSize": 10,
                                               "position": "insideEndTop"}} for m in markers]}}],
                }
                chart = ui.echart(opts).classes("w-full h-60")
                chart.on("click", lambda e, c=h["code"]: self.state.drill(codes=(c,)))

    async def _clicked(self, e):
        row = e.args.get("data") or {}
        if not row.get("billing_code"):
            return
        drill = {"codes": (row["billing_code"],)}
        by = self.breakdown.value
        if by in BREAKDOWN_FILTER and row.get("breakdown") is not None:
            value = row.get("_pos_group") if by == "place_of_service" else row["breakdown"]
            drill[BREAKDOWN_FILTER[by]] = (value,)
        await self.state.drill(**drill)


def marker_color(name, state):
    tag_type = next((t["tag_type"] for t in state.options["tag_names"] if t["tag_name"] == name), "platform")
    palette = ["#7C3AED", "#DB2777", "#2563EB", "#CA8A04", "#EA580C", "#0891B2", "#16A34A", "#9333EA"]
    names = sorted(state.pinned)
    base = palette[names.index(name) % len(palette)] if name in names else theme.PLATFORM_COLOR
    return base if tag_type == "platform" or name in names else theme.SYSTEM_COLOR


class Benchmarks(Section):
    key, anchor, title = "benchmarks", "benchmarks", "Benchmarks"
    subtitle = ("Each tagged platform / health system's rates next to the market under the same filters. One line per "
                "distinct rate. Click a line to open its rows.")

    def body(self):
        names = {t["tag_name"]: t["tag_name"] for t in self.state.options["tag_names"]}
        with ui.row().classes("items-center gap-3 w-full"):
            pin = ui.select(names, label="Pinned entities (histograms, map, this table)", multiple=True,
                            with_input=True, value=list(self.state.pinned),
                            on_change=lambda e: self.state.set_pinned(e.value))
            pin.props("dense outlined use-chips").classes("w-96")
            self.only_pinned = ui.switch("Pinned entities only (switch off to see all tagged entities)", value=True,
                                         on_change=lambda: asyncio.create_task(self.safe_refresh())).props("dense")
            self.count = ui.label().classes("section-sub")
        self.grid = grid([
            col("tag_name", "Entity", width=170, pinned="left"), col("tag_type", "Type", width=120),
            col("billing_code", "Code", width=90), col("rate", "Rate", "money", 100),
            col("pct_below", "% below", "pct", 100), col("pct_at_or_below", "% at or below", "pct", 120),
            col("n_market", "Market n", "int", 105), col("market_median", "Market median", "money", 125),
            col("networks", "Networks", "list", 240), col("setting", "Setting", width=170),
            col("provider_type_group", "Provider type", width=130), col("n_npis", "# NPIs", "int", 95),
            col("n_tins", "# TINs", "int", 90)], height="h-96")
        self.grid.on("cellClicked", self._clicked)

    async def refresh(self):
        f = self.state.filters
        if self.only_pinned.value and self.state.pinned:
            f = f.replace(tag_names=self.state.pinned)
        res = await self.state.call(self.state.backend.benchmarks, f)
        rows = [dict(r, setting=S.POS_GROUP_LABELS.get(r["pos_group"], r["pos_group"]),
                     tag_type="Platform" if r["tag_type"] == "platform" else "Health system") for r in res["rows"]]
        self.count.set_text(f"{len(rows):,} lines · market = {theme.UNIT_LABEL[res['counting_unit']]}")
        set_rows(self.grid, rows)

    async def _clicked(self, e):
        r = e.args.get("data") or {}
        if r.get("tag_name"):
            await self.state.drill(tag_names=(r["tag_name"],), codes=(r["billing_code"],),
                                   pos_groups=(r["pos_group"],) if r.get("pos_group") else (),
                                   provider_types=(r["provider_type_group"],))
