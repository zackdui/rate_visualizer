"""The global filter bar (rates_tool_plan.md section 6): type-to-search multi-selects, text search, toggles, and the
counting unit. It edits PageState.filters; every section refreshes from that one Filters object."""
from nicegui import ui
from ..backend import schema as S

TOGGLES = [  # (Filters field, label, tooltip)
    ("exclude_zero", "Exclude $0 rates", "Hide rates that are exactly $0"),
    ("exclude_invalid_npi", "Exclude NPI = 0", "Hide placeholder NPIs (e.g. 0) listed in the files"),
    ("texas_files_only", "Texas files only", "Only rates from BCBSTX's own Texas network files"),
    ("dollar_rates_only", "Dollar rates only", "Negotiated and fee-schedule dollar amounts (not % of billed charges)"),
    ("exclude_expired", "Exclude expired", "Hide rates whose contract expired before the snapshot date"),
    ("exclude_platforms_from_market", "Exclude platforms from market", "Leave platform TINs out of market statistics"),
    ("bh_providers_only", "Behavioral health providers only",
     "Only providers whose type can bill the code (ghost-rate rule, plan 8.9). Recommended: ghost rates are common"),
]


def _multi(label, options, field, state, width="w-44"):
    sel = ui.select(options, label=label, multiple=True, with_input=True, clearable=True,
                    value=list(getattr(state.filters, field)),
                    on_change=lambda e: state.update(**{field: tuple(e.value or ())}))
    sel.props("dense outlined use-chips options-dense").classes(width)
    return sel


def build(state):
    o = state.options
    controls = {}
    with ui.expansion(value=False).classes("w-full").props("dense header-class='text-sm font-medium'") as exp:
        with exp.add_slot("header"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.icon("filter_list")
                summary = ui.label().classes("text-sm")
        with ui.row().classes("w-full items-start gap-2 flex-wrap"):
            codes = {c["value"]: c["label"] for c in o["codes"]}
            controls["codes"] = _multi("Code", codes, "codes", state, "w-56")
            controls["provider_types"] = _multi("Provider type", o["provider_types"], "provider_types", state)
            controls["networks"] = _multi("Network", o["networks"], "networks", state, "w-52")
            controls["states"] = _multi("State", [s["value"] for s in o["states"]], "states", state, "w-32")
            controls["cities"] = _multi("City (TX)", [c["value"] for c in o["cities"]], "cities", state)
            controls["zips"] = _multi("ZIP (TX)", {z["value"]: f"{z['value']} {z['city'] or ''}".strip()
                                                  for z in o["zips"]}, "zips", state, "w-36")
            pos = {p["value"]: f"{p['value']} · {p['label']}" for p in o["places_of_service"]}
            controls["places_of_service"] = _multi("Place of service", pos, "places_of_service", state, "w-56")
            controls["pos_groups"] = _multi("Setting", {p["value"]: p["label"] for p in o["pos_groups"]},
                                            "pos_groups", state, "w-48")
            controls["tag_types"] = _multi("Entity tag", {"platform": "Platform", "health_system": "Health system",
                                                          "none": "None (untagged)"}, "tag_types", state)
            names = {t["tag_name"]: f"{t['tag_name']} ({'platform' if t['tag_type'] == 'platform' else 'health system'})"
                     for t in o["tag_names"]}
            controls["tag_names"] = _multi("Tagged entity", names, "tag_names", state, "w-60")
            search = ui.input("Billing entity / NPI / name", on_change=lambda e: state.update(search=e.value or ""))
            search.props("dense outlined clearable debounce=400").classes("w-64")
            controls["search"] = search
        with ui.row().classes("w-full items-center gap-x-4 gap-y-1 flex-wrap mt-1"):
            for field, label, tip in TOGGLES:
                sw = ui.switch(label, value=getattr(state.filters, field),
                               on_change=lambda e, f=field: state.update(**{f: bool(e.value)}))
                sw.props("dense").classes("text-sm").tooltip(tip)
                controls[field] = sw
            ui.separator().props("vertical")
            ui.label("Count").classes("text-sm text-grey-7")
            unit = ui.toggle({"tin": "per TIN", "npi": "per NPI", "row": "per row"}, value=state.filters.counting_unit,
                             on_change=lambda e: state.update(counting_unit=e.value or "tin"))
            unit.props("dense no-caps unelevated toggle-color=primary")
            controls["counting_unit"] = unit
            conf = ui.select({"confirmed": "Tags: confirmed", "high": "Tags: high+", "medium": "Tags: medium+"},
                             value=state.filters.min_tag_confidence,
                             on_change=lambda e: state.update(min_tag_confidence=e.value or "high"))
            conf.props("dense outlined options-dense").classes("w-40").tooltip(
                "Minimum confidence for a TIN to count as a tagged platform / health system (see entity_matching.md)")
            controls["min_tag_confidence"] = conf
            ui.button("Reset", icon="restart_alt", on_click=lambda: _reset(state, controls)).props("flat dense no-caps")

    def refresh_summary():
        f = state.filters
        active = [k for k in ("codes", "provider_types", "networks", "states", "cities", "zips", "places_of_service",
                              "pos_groups", "tag_types", "tag_names") if getattr(f, k)]
        if f.search:
            active.append("search")
        changed = [k for k, _, _ in TOGGLES if getattr(f, k) != getattr(type(f)(), k)]
        summary.set_text(f"Filters · {len(active)} active" + (f" · {len(changed)} toggle(s) changed" if changed else "")
                         + f" · counting {f.counting_unit.upper()}")

    state.on_filters_changed.append(refresh_summary)
    refresh_summary()
    return controls


def _reset(state, controls):
    defaults = type(state.filters)()
    state.filters = defaults
    for field, ctl in controls.items():
        value = getattr(defaults, field)
        ctl.set_value(list(value) if isinstance(value, tuple) else value)
    state.reset()
