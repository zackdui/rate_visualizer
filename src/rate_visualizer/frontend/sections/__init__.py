"""Page sections, in the order of rates_tool_plan.md section 5. Each is a Section (frontend/state.py)."""
from nicegui import ui
from .. import theme

MONEY = ':valueFormatter'
MONEY_FMT = "p => p.value == null ? '' : '$' + Number(p.value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})"
PCT_FMT = "p => p.value == null ? '' : Number(p.value).toFixed(1) + '%'"
INT_FMT = "p => p.value == null ? '' : Number(p.value).toLocaleString('en-US')"
LIST_FMT = "p => Array.isArray(p.value) ? p.value.join(', ') : (p.value ?? '')"
RATIO_FMT = "p => p.value == null ? '' : Number(p.value).toFixed(2) + '×'"


def col(field, header, kind=None, width=None, **extra):
    c = {"field": field, "headerName": header, "sortable": True, "filter": True, "resizable": True}
    fmt = {"money": MONEY_FMT, "pct": PCT_FMT, "int": INT_FMT, "list": LIST_FMT, "ratio": RATIO_FMT}.get(kind)
    if fmt:
        c[MONEY] = fmt
    if kind in ("money", "pct", "int", "ratio"):
        c["type"] = "rightAligned"
    if width:
        c["width"] = width
    c.update(extra)
    return c


def grid(columns, height="h-96", **options):
    g = ui.aggrid({"columnDefs": columns, "rowData": [], "defaultColDef": {"minWidth": 90},
                   "rowSelection": {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True},
                   "suppressCellFocus": True, **options}, theme="quartz").classes(f"w-full {height}")
    return g


def set_rows(g, rows, columns=None):
    if columns is not None:
        g.options["columnDefs"] = columns
    g.options["rowData"] = rows
    g.update()


class Pager:
    """Prev / next page controls for server-side paging."""

    def __init__(self, on_change):
        self.page, self.total, self.page_size = 1, 0, 200
        self.on_change = on_change
        with ui.row().classes("items-center gap-2 text-sm"):
            self.prev = ui.button(icon="chevron_left", on_click=lambda: self._go(-1)).props("flat dense round")
            self.label = ui.label("")
            self.next = ui.button(icon="chevron_right", on_click=lambda: self._go(1)).props("flat dense round")

    async def _go(self, step):
        pages = max(1, -(-self.total // self.page_size))
        new = min(max(1, self.page + step), pages)
        if new != self.page:
            self.page = new
            await self.on_change()

    def show(self, total, page, page_size):
        self.total, self.page, self.page_size = total, page, page_size
        start = 0 if total == 0 else (page - 1) * page_size + 1
        self.label.set_text(f"{theme.num(start)}–{theme.num(min(page * page_size, total))} of {theme.num(total)}")
        self.prev.set_enabled(page > 1)
        self.next.set_enabled(page * page_size < total)
