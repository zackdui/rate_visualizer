"""Per-visitor page state: the current Filters, pinned benchmark entities, and refreshing every section.

Sections register themselves and implement `async refresh()`. The frontend never touches SQL: every data call goes
through the Backend (rate_visualizer.backend), run in a worker thread so the page stays responsive.
"""
import asyncio, logging
from nicegui import run, ui
from ..backend import Filters

log = logging.getLogger("rates_tool")
DEFAULT_PINNED = ("Headway", "Talkiatry")


class PageState:
    def __init__(self, backend):
        self.backend = backend
        self.options = backend.filter_options()
        self.filters = Filters()
        self.pinned = tuple(p for p in DEFAULT_PINNED if p in {t["tag_name"] for t in self.options["tag_names"]})
        self.sections = []
        self.explorer = None       # set by the Rate Explorer section (drill-down target)
        self.provider = None       # Provider Profile section
        self.entity = None         # Billing Entity Profile section
        self._task = None
        self.on_filters_changed = []

    async def call(self, fn, *args, **kwargs):
        """Run a backend call off the event loop."""
        return await run.io_bound(fn, *args, **kwargs)

    # ---- filters ---------------------------------------------------------------------------------------------
    def update(self, **changes):
        self.filters = self.filters.replace(**changes)
        for cb in self.on_filters_changed:
            cb()
        self.schedule()

    def reset(self):
        self.filters = Filters()
        for cb in self.on_filters_changed:
            cb()
        self.schedule()

    def set_pinned(self, names):
        self.pinned = tuple(names or ())
        self.schedule(only=("code_summary", "benchmarks", "map"))

    def schedule(self, delay=0.35, only=None):
        """Debounced refresh: rapid filter clicks cause one refresh."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._refresh_later(delay, only))

    async def _refresh_later(self, delay, only):
        try:
            await asyncio.sleep(delay)
            await self.refresh_all(only)
        except asyncio.CancelledError:
            pass

    PRIORITY = ("code_summary", "benchmarks", "explorer")   # above the fold: load these first

    async def refresh_all(self, only=None):
        targets = [s for s in self.sections if only is None or s.key in only]
        first = [s for s in targets if s.key in self.PRIORITY]
        rest = [s for s in targets if s.key not in self.PRIORITY]
        for group in (first, rest):
            results = await asyncio.gather(*(s.safe_refresh() for s in group), return_exceptions=True)
            for s, r in zip(group, results):
                if isinstance(r, Exception):
                    log.error("refresh failed for %s: %r", s.key, r)

    # ---- drill-down / navigation ---------------------------------------------------------------------------------
    async def drill(self, **overrides):
        """Open the exact rows behind an aggregate in the Rate Explorer."""
        if self.explorer:
            await self.explorer.open_drill(overrides)
            scroll_to("rate-explorer")

    async def open_provider(self, npi):
        if self.provider:
            await self.provider.open(npi)
            scroll_to("provider-profile")

    async def open_entity(self, tin):
        if self.entity:
            await self.entity.open(tin)
            scroll_to("billing-entity-profile")


SCROLL_JS = """(anchor) => {
  const el = document.getElementById(anchor); if (!el) return;
  const header = document.querySelector('.q-header');
  const top = el.getBoundingClientRect().top + window.scrollY - (header ? header.offsetHeight : 0) - 12;
  window.scrollTo({top, behavior: 'smooth'});
}"""


def scroll_to(anchor):
    """Scroll to a section, leaving room for the sticky header (whose height depends on the filter bar)."""
    ui.run_javascript(f"({SCROLL_JS})('{anchor}')")


class Section:
    """Base for page sections: a card with a title, subtitle, counting-unit chip, and a spinner while loading."""
    key = ""
    anchor = ""
    title = ""
    subtitle = ""
    shows_unit = True

    def __init__(self, state: PageState):
        self.state = state
        state.sections.append(self)
        self.loading = None

    def build(self):
        from . import theme
        with ui.element("section").props(f"id={self.anchor}").classes("w-full"):
            with ui.card().classes("section-card w-full p-5 gap-3"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(self.title).classes("section-title")
                        if self.subtitle:
                            ui.label(self.subtitle).classes("section-sub")
                    with ui.row().classes("items-center gap-2"):
                        self.loading = ui.spinner(size="sm").classes("hidden")
                        if self.shows_unit:
                            self.unit_holder = ui.row().classes("gap-0")
                            self._draw_unit()
                self.body()
        return self

    def _draw_unit(self):
        from . import theme
        self.unit_holder.clear()
        with self.unit_holder:
            theme.unit_chip(self.state.filters.counting_unit)

    def body(self):
        raise NotImplementedError

    async def refresh(self):
        raise NotImplementedError

    async def safe_refresh(self):
        self.loading.classes(remove="hidden")
        try:
            if self.shows_unit:
                self._draw_unit()
            await self.refresh()
        finally:
            self.loading.classes(add="hidden")
