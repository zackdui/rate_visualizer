"""Look and feel: one teal accent, Inter font, light/dark, and number formatting shared by every section."""
from nicegui import ui

ACCENT = "#0F766E"
ACCENT_LIGHT = "#14B8A6"
PLATFORM_COLOR = "#7C3AED"      # tagged platforms (benchmark markers, map rings)
SYSTEM_COLOR = "#EA580C"        # tagged health systems
PERCENTILE_COLORS = ["#0F766E", "#14B8A6", "#A7F3D0", "#FDE68A", "#F59E0B", "#DC2626"]  # low -> high rate
MUTED = "#64748B"

CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  body { font-family: 'Inter', system-ui, sans-serif; background: #F8FAFC; }
  body.body--dark { background: #0B1120; }
  .section-card { border-radius: 14px; border: 1px solid rgba(100,116,139,.18); box-shadow: none; }
  body.body--dark .section-card { background: #111827; }
  section { scroll-margin-top: 9rem; }
  .section-title { font-size: 1.25rem; font-weight: 650; letter-spacing: -0.01em; }
  .section-sub { color: #64748B; font-size: .875rem; }
  .stat-value { font-size: 1.5rem; font-weight: 650; }
  .stat-label { color: #64748B; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  .q-header.app-header { background: #FFFFFF; color: #0F172A; }
  body.body--dark .q-header.app-header { background: #0B1120; color: #E2E8F0; border-color: #1E293B; }
  .app-muted { color: #64748B; }
  body.body--dark .app-muted { color: #94A3B8; }
  .nav-link { font-size: .85rem; font-weight: 500; color: #334155; }
  body.body--dark .nav-link { color: #CBD5E1; }
  .nav-link:hover { opacity: 1; text-decoration: underline; }
  .ag-theme-quartz, .ag-root-wrapper { border-radius: 10px; }
  .q-field--dense .q-field__control { min-height: 36px; }
</style>
"""


def apply():
    ui.colors(primary=ACCENT, secondary=ACCENT_LIGHT, accent=PLATFORM_COLOR)
    ui.add_head_html(CSS)


def money(v, digits=2):
    return "—" if v is None else f"${v:,.{digits}f}"


def pct(v, digits=1):
    return "—" if v is None else f"{v:.{digits}f}%"


def num(v):
    return "—" if v is None else f"{v:,}"


def ratio(v):
    return "—" if v is None else f"{v:.2f}×"


UNIT_LABEL = {"tin": "per billing entity (TIN)", "npi": "per provider (NPI)", "row": "per rate row"}


def unit_chip(unit):
    return ui.badge(f"Counting {UNIT_LABEL[unit]}", color="teal-1", text_color="teal-10").props("rounded")
