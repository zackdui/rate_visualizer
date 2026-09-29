"""8.5 Map: one marker per ZIP centroid, coloured by the market percentile of its median rate for one code."""
import asyncio, json, math
from nicegui import ui
from ...backend import schema as S
from .. import theme
from ..state import Section
from . import col, grid, set_rows
from .summary import marker_color

TEXAS = (31.2, -99.3)
BANDS = [(20, "0–20th"), (40, "20–40th"), (60, "40–60th"), (80, "60–80th"), (95, "80–95th"), (101, "95th+")]


def band_color(p):
    if p is None:
        return "#94A3B8"
    for (limit, _), color in zip(BANDS, theme.PERCENTILE_COLORS):
        if p < limit:
            return color
    return theme.PERCENTILE_COLORS[-1]


class MapView(Section):
    key, anchor, title = "map", "map", "Map"
    subtitle = ("Providers at their NPPES practice ZIP (Census ZCTA centroid). Colour = where the ZIP's median rate sits "
                "in the market. Click a marker to list its providers.")

    def __init__(self, state):
        super().__init__(state)
        self.points, self.center, self.zoom = [], TEXAS, 6

    def body(self):
        codes = {c["value"]: c["label"] for c in self.state.options["codes"]}
        with ui.row().classes("items-center gap-3 w-full flex-wrap"):
            self.code = ui.select(codes, value="90837", label="Code on the map",
                                  on_change=lambda: asyncio.create_task(self.safe_refresh()))
            self.code.props("dense outlined options-dense").classes("w-72")
            with ui.row().classes("items-center gap-2 text-xs"):
                for (_, label), color in zip(BANDS, theme.PERCENTILE_COLORS):
                    ui.html(f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
                            f'background:{color}"></span> {label}')
            self.outline = ui.select({"pinned": "Pinned entities", "platform": "Any platform",
                                      "health_system": "Any health system", "none": "Nothing"},
                                     value="pinned", label="Outline ZIPs that have",
                                     on_change=lambda: asyncio.create_task(self.safe_refresh()))
            self.outline.props("dense outlined options-dense").classes("w-52")
            self.note = ui.label().classes("section-sub")
        with ui.row().classes("w-full gap-4 no-wrap items-stretch"):
            self.map_holder = ui.column().classes("grow min-w-0")
            with ui.column().classes("w-96 gap-2"):
                self.detail = ui.column().classes("gap-1")
                with self.detail:
                    ui.label("Click a marker to see its providers.").classes("section-sub")
                self.who = grid([col("provider_name", "Provider", width=170), col("median_rate", "Median", "money", 95),
                                 col("provider_type_group", "Type", width=110), col("tag_name", "Tag", width=110)],
                                height="h-80")
                self.who.on("cellClicked", lambda e: self.state.open_provider((e.args.get("data") or {}).get("npi")))
        self._new_map()

    def _new_map(self):
        with self.map_holder:
            self.map = ui.leaflet(center=self.center, zoom=self.zoom).classes("w-full h-[560px] rounded-lg")
        self.map.on("map-click", self._clicked)
        self.map.on("map-moveend", self._moved)
        self.map.on("map-zoomend", self._moved)

    def _moved(self, e):
        c = e.args.get("center")
        if c:
            self.center = (c[0], c[1]) if isinstance(c, list) else (c["lat"], c["lng"])
        if e.args.get("zoom") is not None:
            self.zoom = e.args["zoom"]

    def _outline(self, p):
        mode = self.outline.value
        if mode == "pinned":
            hits = sorted(set(self.state.pinned) & set(p["tag_names"] or []))
            return marker_color(hits[0], self.state) if hits else None
        if mode == "platform" and p["has_platform"]:
            return theme.PLATFORM_COLOR
        if mode == "health_system" and p["has_health_system"]:
            return theme.SYSTEM_COLOR
        return None

    async def refresh(self):
        f = self.state.filters
        res = await self.state.call(self.state.backend.map_points, f, self.code.value, 2500)
        self.points = res["points"]
        self.note.set_text(f"{len(self.points):,} ZIPs" + (" (capped)" if res["capped"] else "") +
                           f" · {res['unmapped_providers']:,} providers without a map point")
        biggest = max([p["n_providers"] for p in self.points] or [1])
        data = []
        for p in self.points:
            ring = self._outline(p)
            style = {"radius": round(4 + 12 * math.sqrt(p["n_providers"] / biggest), 1),
                     "fillColor": band_color(p["market_percentile"]), "fillOpacity": .8,
                     "color": ring or "#FFFFFF", "weight": 2 if ring else 1, "opacity": .7 if ring else 1}
            tip = (f"ZIP {p['zip5'] or '—'} {p['city'] or ''}<br>{p['n_providers']:,} providers · median "
                   f"{theme.money(p['median_rate'])} · {theme.pct(p['market_percentile'], 0)} pct")
            if p["tag_names"]:
                tip += "<br>" + ", ".join(p["tag_names"][:4]) + (" …" if len(p["tag_names"]) > 4 else "")
            data.append([p["lat"], p["lon"], style, tip])
        await self.map.initialized()
        # draw every marker in the browser in one message (thousands of separate layer messages are slow)
        self.map.client.run_javascript(f"""
            const c = getElement({self.map.id}); const m = c.map;
            if (c._rtLayer) c._rtLayer.remove();
            c._rtLayer = L.layerGroup().addTo(m);
            for (const [lat, lon, style, tip] of {json.dumps(data)}) {{
                L.circleMarker([lat, lon], style).bindTooltip(tip).addTo(c._rtLayer);
            }}""")

    async def _clicked(self, e):
        ll = e.args.get("latlng") or {}
        lat, lon = ll.get("lat"), ll.get("lng")
        if lat is None or not self.points:
            return
        best = min(self.points, key=lambda p: (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2)
        if (best["lat"] - lat) ** 2 + (best["lon"] - lon) ** 2 > (4.0 / 2 ** self.zoom) ** 2:
            return
        self.detail.clear()
        with self.detail:
            ui.label(f"ZIP {best['zip5'] or '—'} · {best['city'] or ''}").classes("font-semibold")
            ui.label(f"{best['n_providers']:,} providers · {best['n_tins']:,} TINs · median "
                     f"{theme.money(best['median_rate'])} · {theme.pct(best['market_percentile'], 0)} percentile")\
                .classes("text-sm")
            if best["tag_names"]:
                ui.label("Tagged: " + ", ".join(best["tag_names"])).classes("text-sm")
        rows = await self.state.call(self.state.backend.zip_providers, self.state.filters, best["lat"], best["lon"],
                                     self.code.value)
        set_rows(self.who, rows)
