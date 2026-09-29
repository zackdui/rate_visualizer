"""The website: one NiceGUI page (rates_tool_plan.md section 5), backed only by rate_visualizer.backend.

Startup: the backend is opened in the background (download + verify site.duckdb from R2, or SITE_DB_PATH, then warm
up). /healthz returns 503 until that finishes, so Render keeps the previous instance serving until the new one is ready.
"""
import asyncio, logging, os, traceback
from fastapi.responses import JSONResponse
from nicegui import app, run, ui
from ..backend import open_backend
from . import filterbar, theme
from .sections.composition import RateTypeComposition
from .sections.map_view import MapView
from .sections.profiles import BillingEntityProfile, ProviderProfile
from .sections.quality import DataQuality
from .sections.summary import Benchmarks, CodeSummary
from .sections.tables import PercentageRates, RateExplorer
from .state import PageState, scroll_to

log = logging.getLogger("rates_tool")
TITLE = "BCBSTX Behavioral Health Rates"
SECTIONS = [  # (anchor, nav label, class), in plan order
    ("code-summary", "Code Summary", CodeSummary),
    ("benchmarks", "Benchmarks", Benchmarks),
    ("rate-explorer", "Rate Explorer", RateExplorer),
    ("percentage-rates", "Percentage Rates", PercentageRates),
    ("map", "Map", MapView),
    ("rate-type-composition", "Rate Types", RateTypeComposition),
    ("provider-profile", "Provider Profile", ProviderProfile),
    ("billing-entity-profile", "Billing Entity", BillingEntityProfile),
    ("data-quality", "Data Quality", DataQuality),
]
STATUS = {"backend": None, "error": None, "loading": False}


async def load_backend():
    if STATUS["loading"] or STATUS["backend"]:
        return
    STATUS["loading"] = True
    try:
        STATUS["backend"] = await run.io_bound(open_backend, log=log.info)
        log.info("site data ready: %s", STATUS["backend"].meta().get("snapshot_date"))
    except Exception as e:  # noqa: BLE001 - shown on the loading page and /healthz
        STATUS["error"] = f"{type(e).__name__}: {e}"
        log.error("could not load site data:\n%s", traceback.format_exc())
    finally:
        STATUS["loading"] = False


async def watch_event_loop(threshold=0.5):
    """Log whenever the event loop is blocked for more than `threshold` seconds (a slow page for every visitor)."""
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(0.1)
        lag = loop.time() - start - 0.1
        if lag > threshold:
            log.warning("event loop blocked for %.2fs", lag)


app.on_startup(lambda: asyncio.create_task(load_backend()))
app.on_startup(lambda: asyncio.create_task(watch_event_loop()))


@app.get("/healthz")
def healthz():
    b = STATUS["backend"]
    if b is None:
        return JSONResponse({"ok": False, "loading": STATUS["loading"], "error": STATUS["error"]}, status_code=503)
    h = b.health()
    return JSONResponse(h, status_code=200 if h.get("ok") else 503)


@ui.page("/")
async def index():
    theme.apply()
    dark = ui.dark_mode(None)
    b = STATUS["backend"]
    if b is None:
        _loading_page()
        return
    state = PageState(b)
    meta = b.meta()
    ui.page_title(TITLE)
    with ui.header(elevated=False).classes("app-header border-b px-4 py-2 gap-1 flex-col items-stretch"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.icon("insights", size="md").classes("text-primary")
                with ui.column().classes("gap-0"):
                    ui.label(TITLE).classes("text-base font-semibold leading-tight")
                    ui.label("Negotiated in-network rates for outpatient mental-health codes").classes("text-xs app-muted")
                snap = ui.select([meta["snapshot_date"]], value=meta["snapshot_date"], label="Snapshot")
                snap.props("dense outlined disable options-dense").classes("w-40")\
                    .tooltip("Monthly history can be added later; this is the latest BCBSTX index")
            with ui.row().classes("items-center gap-x-4 gap-y-1 flex-wrap justify-end"):
                for anchor, label, _ in SECTIONS:
                    ui.label(label).classes("nav-link whitespace-nowrap cursor-pointer")\
                        .on("click", lambda _, a=anchor: scroll_to(a))
                ui.button(icon="dark_mode", on_click=lambda: dark.set_value(not dark.value))\
                    .props("flat round dense").tooltip("Light / dark")
        filterbar.build(state)
    with ui.column().classes("w-full max-w-[1500px] mx-auto px-4 py-5 gap-5"):
        for _, _, cls in SECTIONS:
            cls(state).build()
        ui.label(f"Source: BCBSTX Transparency in Coverage files, index {meta['snapshot_date']} · NPPES · NUCC · "
                 f"Census ZCTA · built {meta['built_at']} · every number traces to its source file and position")\
            .classes("text-xs text-grey-6 py-4")
    ui.timer(0.05, state.refresh_all, once=True)


def _loading_page():
    with ui.column().classes("w-full h-screen items-center justify-center gap-3"):
        if STATUS["error"]:
            ui.icon("error", size="xl").classes("text-negative")
            ui.label("The site data could not be loaded.").classes("text-lg font-medium")
            ui.label(STATUS["error"]).classes("text-sm text-grey-7 max-w-xl text-center")
        else:
            ui.spinner(size="xl")
            ui.label("Loading the latest rate data…").classes("text-lg font-medium")
            ui.label("This takes under a minute after a restart.").classes("text-sm text-grey-7")
            ui.timer(2.0, lambda: STATUS["backend"] and ui.navigate.reload())


def main(host="127.0.0.1", port=8080):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ui.run(host=host, port=int(port), title=TITLE, reload=False, show=False, favicon="📈",
           storage_secret=os.environ.get("SITE_STORAGE_SECRET"))
