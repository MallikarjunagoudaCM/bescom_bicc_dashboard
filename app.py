"""
app.py
------
BESCOM BICC Utility Manager Dashboard — Plotly Dash 4-tab app.

Stylesheets (Dash auto-loads all CSS files in assets/ alphabetically):
  assets/01_variables.css   — design tokens (colours, radii, shadows, spacing)
  assets/02_base.css        — reset, body, header, tabs, page text, responsive
  assets/03_components.css  — KPI/CC cards, filter bar, buttons, modals
  assets/04_table.css       — DataTable, toolbar, Top-N, column filters, pagination
  assets/05_admin.css       — Admin tab: login panel, upload zone, summary table

Data → db.py (SQLite bicc.db)
Delta load → delta_load.py
Global date-range picker in header applies across ALL tabs.

Run:  python app.py   →  http://localhost:8050
"""

import io, base64, json
from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, dash_table, Input, Output, State, ctx, no_update, ALL
import dash_bootstrap_components as dbc
from db import (
    get_cc_list, get_divisions, get_stations, get_feeders, get_feeder_categories,
    get_overview_kpis, get_daily_trend, get_interruption_table,
    get_division_summary, get_date_range,
    get_db_stats, get_recent_uploads,
    get_daily_trend_agg,        # NEW
    get_division_customers, get_feeder_customers, 
    get_cc_customers,                  # NEW
    get_division_customers_total,      # NEW
    get_station_customers_total,       # NEW
    get_feeder_customers_total,
    invalidate_hierarchy_cache, logger,
    #get_division_saifi_saidi,
    get_period_comparison,      # NEW — Trends: Period Comparison
    get_saidi_saifi_trend,      # NEW — Trends: Reliability Trends & Ranking
    get_repeat_offenders,       # NEW — Trends: Repeat Offenders table
)
from delta_load import delta_insert_tracked, rollback_upload

# ── Plotly colour palette ─────────────────────────────────────────────────────
_PRIMARY  = "#0098d4"
_NAVY     = "#0a2540"
_CYAN     = "#00c0ef"
_ACCENT   = "#245dff"
_DANGER   = "#e03c3c"
_WARNING  = "#e09000"
_SUCCESS  = "#2e8b40"
_SURFACE  = "#ffffff"
_INK      = "#0a2540"
_MUTED    = "#64748b"
_DIVIDER  = "#e2e8f0"
_DIV_PALETTE = [
    "#0098d4","#e03c3c","#2e8b40","#e09000","#7b3fa0",
    "#c0392b","#16a085","#d35400","#2980b9","#8e44ad",
]

# ── Zone label map (display-only; DB values remain BICC-1 / BICC-2) ─────────
ZONE_LABEL = {
    "BICC-1": "BMAZ South Zone",
    "BICC-2": "BMAZ North Zone",
}

BESCOM_LOGO = "/assets/BESCOM.jpg"

# ── App ───────────────────────────────────────────────────────────────────────
app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    assets_folder="assets",
    title="BESCOM BICC General Manager's Dashboard",
    suppress_callback_exceptions=True,
)

from flask import jsonify, request
import time
@app.server.route("/debug/cache-status")
def cache_status():
    if not DEBUG:
        return jsonify({"error": "Not available in production"}), 403

    from db import _HIERARCHY_CACHE, _CACHE_TTL, get_cache_stats
    now    = time.time()
    stats  = get_cache_stats()
    keys   = {}

    for key, entry in _HIERARCHY_CACHE.items():
        age  = round(now - entry["ts"], 1)
        keys[key] = {
            "status"     : "FRESH" if age < _CACHE_TTL else "STALE",
            "age_seconds": age,
            "expires_in" : round(_CACHE_TTL - age, 1),
            "entry_count": len(entry["val"]) if isinstance(entry["val"], list) else 1,
            "sample"     : list(entry["val"])[:3] if isinstance(entry["val"], list)
                           else str(entry["val"])[:80],
        }

    return jsonify({
        "stats" : stats,     # hits, misses, stales, hit_rate_pct
        "keys"  : keys,
    })
    
@app.server.route("/debug/cache-validate")
def cache_validate():
    if not DEBUG:
        return jsonify({"error": "Not available in production"}), 403
    try:
        from db import validate_cache_integrity
        mismatches = validate_cache_integrity()
        return jsonify({
            "status"          : "OK" if not mismatches else "MISMATCH DETECTED",
            "mismatch_count"  : len(mismatches),
            "mismatches"      : mismatches,   # now fully plain dicts/lists
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
server = app.server

from config import (
    MAX_OVERVIEW_DAYS, MAX_TABLE_ROWS_UI,
    BICC_ADMIN_GROUP,
    DB_PATH, DEBUG, HOST, PORT,
    KPI_EVENTS_WARN, KPI_EVENTS_DANGER,
    KPI_AVG_EVENTS_WARN, KPI_AVG_EVENTS_DANGER,
    KPI_CUST_WARN, KPI_CUST_DANGER,
    KPI_CMI_WARN, KPI_CMI_DANGER,
    KPI_UNSCHED_WARN, KPI_UNSCHED_DANGER,
    KPI_MINS_WARN, KPI_MINS_DANGER,
    MAX_ROLLBACK_VISIBLE,       # NEW
    TREND_ANOMALY_ZSCORE,       # NEW
    TOTAL_CUSTOMERS,             # NEW
    KPI_SAIDI_WARN, KPI_SAIDI_DANGER,
    KPI_SAIDI_WARN_CC, KPI_SAIDI_DANGER_CC,
    KPI_SAIFI_WARN,     
    KPI_SAIFI_DANGER,   
    KPI_SAIFI_WARN_CC,  
    KPI_SAIFI_DANGER_CC,
)

from concurrent.futures import ThreadPoolExecutor  #Threadpool execution of DB Calls

# ── Authentik SSO identity (via outpost-injected headers) ───────────────────
# The Authentik outpost sits in front of this app and forwards identity as
# plain request headers on every request. These are only trustworthy because
# Flask is bound to 127.0.0.1 and unreachable except through the outpost.
def current_authentik_groups() -> list[str]:
    raw = request.headers.get("X-authentik-groups", "")
    return [g.strip() for g in raw.split(",") if g.strip()]

def is_bicc_admin() -> bool:
    return BICC_ADMIN_GROUP in current_authentik_groups()

#--------------Helpers SAIDI -------------------------------------------
def resolve_saidi_base(cc, div, stn, fdrs) -> tuple[int, str]:
    """
    Returns (denominator: int, scope_label: str)
    based on the most granular filter currently active.
    Falls back to TOTAL_CUSTOMERS global if no DB match.
    """
    fdrs_list = fdrs if isinstance(fdrs, list) else ([fdrs] if fdrs else [])
    div_list  = div  if isinstance(div,  list) else ([div]  if div  else [])
    stn_val   = stn  if isinstance(stn,  str)  else (stn[0] if stn else None)

    if fdrs_list:
        base  = get_feeder_customers_total(fdrs_list)
        label = f"{len(fdrs_list)} feeder(s)"
    elif stn_val:
        base  = get_station_customers_total([stn_val])
        label = f"Station: {stn_val}"
    elif div_list:
        base  = get_division_customers_total(div_list)
        label = f"Division: {', '.join(div_list)}"
    elif cc:
        cc_key   = cc[0] if isinstance(cc, list) and len(cc) == 1 else cc
        base     = get_cc_customers(cc_key) if not isinstance(cc_key, list) else TOTAL_CUSTOMERS
        zone_str = " & ".join(ZONE_LABEL.get(z, z) for z in cc) if isinstance(cc, list) else ZONE_LABEL.get(cc, cc)
        label    = f"Zone: {zone_str}"
    else:
        base  = TOTAL_CUSTOMERS
        label = "All zones (config)"

    # Fallback to config if DB table not yet seeded
    if base == 0:
        base  = TOTAL_CUSTOMERS
        label += " (config fallback)"

    return base, label
    
def _get_db_date_bounds():
    """Read fresh MIN/MAX event_date from DB each time — never stale."""
    try:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM interruption_events"
        ).fetchone()
        con.close()
        mn = row[0] if row and row[0] else _min_dt
        mx = row[1] if row and row[1] else _max_dt
        return str(mn), str(mx)
    except Exception:
        return _min_dt, _max_dt
        
def _latest_month_window():
    """
    Returns (start_date, end_date) for the latest month available in DB.
    Example:
      DB max date = 2026-06-18  -> returns ('2026-06-01', '2026-06-18')
      DB max date = 2026-05-31  -> returns ('2026-05-01', '2026-05-31')
    """
    db_min, db_max = _get_db_date_bounds()
    mx = pd.to_datetime(db_max)
    start = mx.replace(day=1)
    return str(start.date()), str(mx.date())


def _clamp_date_window(start, end, max_days=MAX_OVERVIEW_DAYS):
    db_min, db_max = _get_db_date_bounds()
    s = pd.to_datetime(start) if start else pd.to_datetime(db_min)
    e = pd.to_datetime(end)   if end   else pd.to_datetime(db_max)
    if s > e:
        s, e = e, s
    if (e - s).days + 1 > max_days:
        s = e - pd.Timedelta(days=max_days - 1)
    if s < pd.to_datetime(db_min):
        s = pd.to_datetime(db_min)
    if e > pd.to_datetime(db_max):
        e = pd.to_datetime(db_max)
    return str(s.date()), str(e.date())



def _first_or_none(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v

def _label_value(v):
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return v

# ── DataTable column/row style (no colours — all in CSS) ──────────────────────
_TABLE_BASE = dict(
    page_size=20,
    filter_action="none",          # custom searchable dropdown filters replace native
    sort_action="custom",          # clickable header sort via sort_by callback
    sort_mode="multi",
    row_selectable="multi",
    style_table={"overflowX": "auto", "minWidth": "100%"},
    style_header={
        "border": "none",
        "padding": "10px 14px",
        "cursor": "pointer",
        "userSelect": "none",
        "fontWeight": "700",
        "fontSize": "12px",
        "textTransform": "uppercase",
        "letterSpacing": "0.04em",
        "color": _NAVY,
        "backgroundColor": "#eaf4fb",
        "borderBottom": f"2px solid {_PRIMARY}",
        "whiteSpace": "nowrap",
        "position": "sticky",
        "top": 0,
        "zIndex": 3,
    },
    style_cell={
        "fontSize": "13px", "padding": "9px 14px",
        "border": "none", "whiteSpace": "normal",
        "height": "auto", "maxWidth": "240px",
        "overflow": "hidden", "textOverflow": "ellipsis",
    },
    style_cell_conditional=[
        {
            "if": {"column_id": "Date"},      # freeze Date column
            "position": "sticky",
            "left": 0,
            "zIndex": 2,
            "backgroundColor": "#ffffff",
            "minWidth": "100px",
        },
        {
            "if": {"column_id": "Trouble Time"},
            "position": "sticky", "left": "100px",        # ← offset by Date column width
            "z-index": 2, 
            "background-color": "#ffffff", 
            "min-width": "90px"
        },
        {
            "if": {"column_id": "subdivision"},   # freeze subdivision column too
            "position": "sticky",
            "left": 0,
            "zIndex": 2,
            "backgroundColor": "#ffffff",
            "minWidth": "130px",
        },
    ],

    style_header_conditional=[
        {
            "if": {"column_id": "Date"},
            "position": "sticky",
            "left": 0,
            "zIndex": 5,
            "backgroundColor": "#eaf4fb",
        },
        {
            "if": {"column_id": "Trouble Time"},
            "position": "sticky", 
            "left": "100px",
            "z-index": 5, 
            "background-color": "#eaf4fb"
        },
        {
            "if": {"column_id": "subdivision"},
            "position": "sticky",
            "left": 0,
            "zIndex": 5,
            "backgroundColor": "#eaf4fb",
        },
    ],
    style_data_conditional=[
        {"if": {"row_index": "even"}, "backgroundColor": "rgba(0,152,212,.04)"},
        {"if": {"row_index": "odd"},  "backgroundColor": "#ffffff"},
        {"if": {"state": "selected"}, "backgroundColor": "rgba(0,152,212,.18)",
         "color": _NAVY},
    ],
    tooltip_delay=0,
    tooltip_duration=None,
)

# ── Icons ─────────────────────────────────────────────────────────────────────
_ICON = {
    "bolt": "⚡", "clock": "⏱", "chart": "📊",
    "warning": "⚠️", "danger": "🔴", "station": "🏭",
    "people": "👥", "cmi": "📉",
}

def _empty_state(icon: str = "📭", title: str = "No data", 
                  subtitle: str = "Try adjusting your filters or date range.") -> html.Div:
    """Reusable empty state component."""
    return html.Div([
        html.Div(icon, style={"fontSize": "36px", "marginBottom": "8px"}),
        html.H6(title, style={"color": "#0a2540", "fontWeight": "700", "margin": "0 0 4px"}),
        html.P(subtitle, style={"color": "#64748b", "fontSize": "13px", "margin": 0}),
    ], style={
        "display": "flex", "flexDirection": "column", "alignItems": "center",
        "justifyContent": "center", "textAlign": "center",
        "padding": "40px 24px", "minHeight": "180px",
        "background": "#f8fbff", "borderRadius": "10px",
        "border": "1px dashed #cde4f5",
    })

# ── Shared helpers ────────────────────────────────────────────────────────────

def _threshold_class(value: float, warn: float, danger: float) -> str:
    if value >= danger:
        return "kpi-thresh-danger"
    if value >= warn:
        return "kpi-thresh-warn"
    return ""

def kpi_card(title, value, sub="", variant="",
             tooltip_lines: list[str] | None = None,
             raw_value: float = 0.0,
             warn: float = 0.0, danger: float = 0.0, pct=None):
    thresh_cls = _threshold_class(raw_value, warn, danger) if (warn or danger) else ""
    card_cls = f"kpi-card {variant} {thresh_cls} h-100".strip()

    badge = None
    if thresh_cls == "kpi-thresh-danger":
        badge = html.Span("● Critical", className="kpi-thresh-badge danger")
    elif thresh_cls == "kpi-thresh-warn":
        badge = html.Span("● Warning", className="kpi-thresh-badge warn")

    tooltip_el = None
    if tooltip_lines:
        tooltip_el = html.Div(
            [html.P(line, className="kpi-tooltip-line") for line in tooltip_lines],
            className="kpi-tooltip-box",
        )
        
    icon_row = html.Div(
        [
            html.Div(_ICON.get(variant, ""), className="kpi-icon"),
            html.Div(                          # ← new wrapper: positions ⓘ + tooltip together
                [
                    html.Span("ℹ", className="kpi-info-icon", tabIndex=0) if tooltip_lines else None,
                    tooltip_el,               # ← tooltip is NOW inside the hover parent
                ],
                className="kpi-tooltip-anchor"  # ← position:relative wrapper
            ) if tooltip_lines else None,
        ],
        className="kpi-icon-row"
    )
    
    progress_bar = None
    if pct is not None:
        fill_pct   = min(float(pct), 100.0)
        track_cls  = (
            "kpi-progress-track danger" if thresh_cls == "kpi-thresh-danger"
            else "kpi-progress-track warn" if thresh_cls == "kpi-thresh-warn"
            else "kpi-progress-track"
        )
        progress_bar = html.Div(
            html.Div(
                className="kpi-progress-fill",
                style={"width": f"{fill_pct:.1f}%"},   # only width stays inline — dynamic value
            ),
            className=track_cls,
        )
    card_inner = html.Div([
        icon_row,
        html.P(title, className="kpi-label"),
        html.H3(value, className="kpi-value"),
        html.P(sub, className="kpi-sub"),
        badge,
    ], className="kpi-card-body")

    return html.Div(card_inner, className=card_cls)

def section_heading(icon, text):
    return html.H5(f"{icon} {text}", className="section-heading")


def hierarchy_dropdowns(cc_id, div_id, stn_id, fdr_id, default_cc=None):
    cat_id = f"{stn_id}-cat"
    return dbc.Row([
        dbc.Col([
            html.Label("Zone"),
            dcc.Dropdown(id=cc_id,
                         options=[{"label": ZONE_LABEL.get(c, c), "value": c} for c in get_cc_list()],
                         value=default_cc, placeholder="All Zones…",
                         searchable=True, clearable=True, multi=True),
        ], xs=12, sm=6, md=2),
        dbc.Col([
            html.Label("Division"),
            dcc.Dropdown(id=div_id, placeholder="Select Division(s)…",
                         searchable=True, clearable=True, multi=True),
        ], xs=12, sm=6, md=2),
        dbc.Col([
            html.Label("Station"),
            dcc.Dropdown(id=stn_id, placeholder="Select Station…",
                         searchable=True, clearable=True),
        ], xs=12, sm=6, md=2),
        dbc.Col([
            html.Label("Feeder Category"),
            dcc.Dropdown(id=cat_id, placeholder="All categories…",
                         searchable=True, clearable=True, multi=True),
        ], xs=12, sm=6, md=3),
        dbc.Col([
            html.Label("Feeder(s)"),
            dcc.Dropdown(id=fdr_id, placeholder="Select Feeder(s)…",
                         searchable=True, clearable=True, multi=True),
        ], xs=12, sm=6, md=3),
    ], className="g-3 mb-3")

def make_table_toolbar(tbl_id_prefix, row_count=0):
    """Top toolbar: title + row count + Top-N selector + export + bar chart + columns."""
    return html.Div([
        html.Span("📋 Interruption Events", className="table-toolbar-title"),
        html.Span(f"{row_count:,} rows", className="row-count-badge",
                  id=f"row-count-{tbl_id_prefix}"),

        # ── Top-N selector ──────────────────────────────────────────────────
        html.Div([
            html.Span("Top", className="topn-label"),
            dcc.Input(
                id=f"topn-input-{tbl_id_prefix}",
                type="number", min=1, step=1,
                placeholder="N",
                debounce=False,
                className="topn-input",
            ),
            html.Span("rows", className="topn-label"),
            dbc.Button("Select", id=f"btn-topn-{tbl_id_prefix}",
                       className="btn-topn"),
        ], className="topn-wrap"),

        dbc.Button("⬇ Export",    id=f"btn-export-{tbl_id_prefix}",
                   className="btn-export"),
        dcc.Download(id=f"download-{tbl_id_prefix}"),
        dbc.Button("📊 Bar Chart", id=f"btn-chart-{tbl_id_prefix}",
                   className="btn-toolbar-action"),
        dbc.Button("🗂 Columns",   id=f"btn-cols-{tbl_id_prefix}",
                   className="btn-toolbar-action"),
    ], className="table-toolbar")


def make_col_modal(tbl_id_prefix, columns):
    """Modal for selecting which columns to display & chart."""
    options = [{"label": c, "value": c} for c in columns]
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Select Columns to Display"),
                        close_button=True),
        dbc.ModalBody([
            dcc.Checklist(
                id=f"col-checklist-{tbl_id_prefix}",
                options=options,
                value=columns,
                labelStyle={"display": "flex", "alignItems": "center",
                            "gap": "8px", "padding": "4px 0",
                            "fontSize": "13px", "cursor": "pointer"},
                inputStyle={"accentColor": _PRIMARY, "width": "14px", "height": "14px"},
            )
        ]),
        dbc.ModalFooter(
            dbc.Button("Apply", id=f"btn-cols-apply-{tbl_id_prefix}",
                       className="btn-apply", n_clicks=0)
        ),
    ], id=f"modal-cols-{tbl_id_prefix}", is_open=False, size="sm",
       backdrop=True, scrollable=True)


def make_chart_modal(tblidprefix):
    return dbc.Modal([
        dbc.ModalHeader(
            html.Div([
                html.Span(id=f"modal-chart-title-{tblidprefix}", children="Chart Selected Data",
                          className="chart-modal-title"),
            ]), close_button=True,
        ),
        dbc.ModalBody([
            dbc.Row([
                dbc.Col([
                    html.Label("X-axis category", className="form-label-lite"),
                    dcc.Dropdown(id=f"chart-x-{tblidprefix}", placeholder="Select column",
                                 clearable=False, className="dropdown-lite"),
                ], md=4),
                dbc.Col([
                    html.Label("Y-axis metric", className="form-label-lite"),
                    dcc.Dropdown(id=f"chart-y-{tblidprefix}", placeholder="Select metric",
                                 clearable=False, className="dropdown-lite"),
                ], md=4),
                dbc.Col([
                    html.Label("Chart Type", className="form-label-lite"),
                    dbc.RadioItems(
                        id=f"chart-type-{tblidprefix}",
                        options=[
                            {"label": "Bar",         "value": "bar"},
                            {"label": "Pie",         "value": "pie"},
                            {"label": "Stacked Bar", "value": "stacked_bar"},
                        ],
                        value="bar", inline=True,
                        className="radio-inline-lite",
                        inputClassName="me-1", labelClassName="me-3",
                    ),
                ], md=3),
                dbc.Col(
                    dbc.Button("Draw", id=f"btn-draw-{tblidprefix}",
                               className="btn-apply mt-4"),
                    md=1, className="d-flex align-items-end",
                ),
            ], className="g-3 mb-3"),
            html.Div([
                dbc.ButtonGroup([
                    dbc.Button("Switch to Bar", id=f"btn-to-bar-{tblidprefix}",
                               size="sm", outline=True, color="primary"),
                    dbc.Button("Switch to Pie", id=f"btn-to-pie-{tblidprefix}",
                               size="sm", outline=True, color="primary"),
                ], className="mb-3"),
            ], id=f"chart-toggle-strip-{tblidprefix}", style={"display": "none"}),
            dcc.Store(id=f"store-chart-agg-{tblidprefix}"),
            html.Div(id=f"modal-chart-{tblidprefix}"),
        ]),
    ], id=f"modal-chart-modal-{tblidprefix}", is_open=False, size="xl",
       backdrop=True, scrollable=True)
def _build_topn_trend_fig(df, metric, warn, danger, top_n=4, height=380):
    fig = go.Figure()
    if df.empty:
        fig.update_layout(**_base_layout(height=height, margin=dict(t=30, b=50, l=60, r=30)))
        return fig

    period_order = sorted(df["period"].unique(),
                           key=lambda p: pd.to_datetime(p+"-01") if "Q" not in p else (int(p[:4]), int(p[-1])))
    labels = [_format_period_label(p, "quarter" if "Q" in p else "month") for p in period_order]

    pivot = df.pivot_table(index="period", columns="division", values=metric, aggfunc="mean").reindex(period_order)
    latest = pivot.iloc[-1].sort_values(ascending=False)
    top_divs = latest.index[:top_n].tolist()

    numerator_col = "total_cmi" if metric == "saidi" else "events"
    overall = df.groupby("period").apply(
        lambda g: g[numerator_col].sum()/g["customers"].sum() if g["customers"].sum() else 0,
        include_groups=False
    ).reindex(period_order)
    others_avg = pivot.drop(columns=top_divs, errors="ignore").mean(axis=1)

    fig.add_bar(x=labels, y=overall.values, name="Zone-wide avg",
                marker_color="#c9d6de", opacity=0.7,
                hovertemplate="Zone-wide avg<br>%{x}: %{y:.4f}<extra></extra>")

    highlight_colors = [_DANGER, _WARNING, "#7b3fa0", _PRIMARY, "#16a085"]
    for i, d in enumerate(top_divs):
        fig.add_scatter(x=labels, y=pivot[d], mode="lines+markers", name=d,
                         line=dict(width=2.5, color=highlight_colors[i % len(highlight_colors)]),
                         hovertemplate=f"{d}<br>%{{x}}: %{{y:.4f}}<extra></extra>")

    fig.add_scatter(x=labels, y=others_avg, mode="lines", name="Other divisions (avg)",
                     line=dict(width=1.5, color=_MUTED, dash="dot"),
                     hovertemplate="Other divisions (avg)<br>%{x}: %{y:.4f}<extra></extra>")

    fig.add_hline(y=warn, line_dash="dot", line_color=_WARNING,
                  annotation_text="Warn", annotation_position="top left")
    fig.add_hline(y=danger, line_dash="dot", line_color=_DANGER,
                  annotation_text="Critical", annotation_position="top left")

    y_title = "SAIDI (minutes / customer)" if metric == "saidi" else "SAIFI (interruptions / customer)"
    fig.update_layout(**_base_layout(
        height=height, margin=dict(t=50, b=50, l=65, r=30),
        hovermode="x unified", legend=dict(orientation="h", y=1.25, x=0),
        yaxis=dict(title=y_title, tickformat=".4f", gridcolor=_DIVIDER, gridwidth=1),
        xaxis=dict(title="Period", tickangle=-20 if len(period_order) > 6 else 0),
    ))
    return fig

#---------------------- NEW HELPER FOR SAIDI/SAIFI HEAT-MAP -------------- 
def _build_heatmap_fig(df, metric, height=520):
    fig = go.Figure()
    if df.empty:
        fig.update_layout(**_base_layout(height=height, margin=dict(t=30, b=50, l=100, r=30)))
        return fig

    period_order = sorted(df["period"].unique(),
        key=lambda p: pd.to_datetime(p+"-01") if "Q" not in p else (int(p[:4]), int(p[-1])))
    labels = [_format_period_label(p, "quarter" if "Q" in p else "month") for p in period_order]

    pivot = df.pivot_table(index="division", columns="period", values=metric, aggfunc="mean").reindex(columns=period_order)
    pivot = pivot.loc[pivot.mean(axis=1).sort_values(ascending=False).index]

    # Rounded display text — independent of color-mapping precision
    text_z = pivot.applymap(lambda v: "" if pd.isna(v) else f"{v:.2f}")

    # Contrast-safe text color per cell: light background -> navy text, dark -> white
    valid = pivot.values[~pd.isna(pivot.values)]
    vmin = valid.min() if valid.size else 0
    vmax = valid.max() if valid.size else 1
    vrange = (vmax - vmin) or 1

    def _text_color(v):
        if pd.isna(v):
            return "#94a3b8"  # muted gray for no-data
        norm = (v - vmin) / vrange
        return "#0a2540" if norm < 0.55 else "#ffffff"  # navy on light half, white on dark half

    font_colors = pivot.applymap(_text_color).values

    fig.add_trace(go.Heatmap(
        z=pivot.values,
        x=labels, y=pivot.index,
        zmin=vmin, zmax=vmax,
        colorscale=[
            [0.0, "#eaf4f4"],   # light neutral — lowest values
            [0.5, "#e09000"],   # amber — mid range
            [1.0, "#8b1e1e"],   # deep red — highest values (darker than _DANGER for contrast headroom)
        ],
        text=text_z.values,
        texttemplate="%{text}",
        textfont=dict(size=11),
        hovertemplate="%{y} — %{x}<br>Value: %{z:.2f}<extra></extra>",
        colorbar=dict(title=metric.upper(), tickformat=".2f"),
        xgap=2, ygap=2,  # subtle cell separation for readability
    ))

    # Per-cell text color requires a manual annotation pass since Heatmap
    # textfont is a single color for the whole trace, not per-cell.
    annotations = []
    for i, div in enumerate(pivot.index):
        for j, per in enumerate(labels):
            v = pivot.values[i, j]
            if pd.isna(v):
                continue
            annotations.append(dict(
                x=per, y=div, text=f"{v:.2f}",
                showarrow=False,
                font=dict(size=11, color=font_colors[i, j]),
            ))

    y_title = "SAIDI (minutes / customer)" if metric == "saidi" else "SAIFI (interruptions / customer)"
    fig.update_layout(**_base_layout(
        height=height, margin=dict(t=40, b=40, l=120, r=30),
        xaxis=dict(title="Period"),
        yaxis=dict(title="Division", autorange="reversed"),
        annotations=annotations,
    ))
    fig.update_layout(coloraxis_colorbar=dict(title=y_title))
    fig.update_traces(texttemplate="")  # suppress default text; annotations render the numbers instead
    return fig

def _format_period_label(period, granularity="month"):
    if granularity == "quarter":
        yr, q = period.split("-Q")
        return f"Q{q} '{yr[2:]}"
    dt = pd.to_datetime(period + "-01")
    return dt.strftime("%b '%y")
       
# =============================================================================
# HEALTH CARD OVER VIEW CC Helper
# =============================================================================

def _cc_health_card(row, agency_df, feeders_interrupted_df):
    """
    Enriched CC card for the Overview tab.

    Parameters
    ----------
    row                    : one row of cc_counts  (control_center, stations, feeders)
    agency_df              : kpi["agency_split"]  DataFrame
    feeders_interrupted_df : kpi["feeders_interrupted"]  DataFrame
    """
    cc             = row.get("control_center", "")
    total_feeders  = int(row.get("feeders",  0))
    total_stations = int(row.get("stations", 0))
    cc_label       = ZONE_LABEL.get(cc, cc) if isinstance(cc, str) else str(cc)

    # ── Feeder health ─────────────────────────────────────────────────────────
    fi_row      = feeders_interrupted_df[feeders_interrupted_df["control_center"] == cc]
    interrupted = int(fi_row["feeders_interrupted"].iloc[0]) if not fi_row.empty else 0
    healthy     = max(total_feeders - interrupted, 0)
    pct_hit     = round(interrupted / total_feeders * 100, 1) if total_feeders > 0 else 0.0
    pct_fill    = min(pct_hit, 100.0)
    bar_color   = (
        "#e03c3c" if pct_hit >= 40
        else "#e09000" if pct_hit >= 20
        else "#2e8b40"
    )

    # ── Agency split ──────────────────────────────────────────────────────────
    ag = agency_df[agency_df["control_center"] == cc].copy()

    def _ag(name):
        r = ag[ag["agency"].str.upper() == name.upper()]
        if r.empty:
            return {"events": 0, "total_mins": 0.0, "total_cmi": 0.0}
        return {"events":     int(r["events"].iloc[0]),
                "total_mins": float(r["total_mins"].iloc[0]),
                "total_cmi":  float(r["total_cmi"].iloc[0])}

    bescom = _ag("BESCOM")
    kptcl  = _ag("KPTCL")

    def _fmt(v, unit="mins"):
        if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
        if v >= 1_000:     return f"{v/1_000:.1f}K"
        return f"{v:,.0f}"

    # ── Build card ────────────────────────────────────────────────────────────
    return html.Div([

        # Header
        html.Div([
            html.P(cc_label, className="cc-label"),
            html.Span(f"{total_feeders:,} feeders · {total_stations} stations",
                      className="cc-universe-badge"),
        ], className="cc-header-row"),

        html.Hr(className="cc-divider"),

        # Feeder health section
        html.Div([
            html.Div([
                html.Span("📡 Feeders Interrupted", className="cc-section-label"),
                html.Span(f"{interrupted:,} / {total_feeders:,}  ({pct_hit}%)",
                          className="cc-feeder-fraction"),
            ], className="cc-feeder-label-row"),

            html.Div(
                html.Div(className="cc-feeder-bar-fill",
                         style={"width": f"{pct_fill:.1f}%", "background": bar_color}),
                className="cc-feeder-bar-track"
            ),

            html.Div([
                html.Span("✅", style={"marginRight": "4px"}),
                html.Span(f"{healthy:,} feeders healthy", className="cc-healthy-label"),
            ], className="cc-healthy-row"),

        ], className="cc-feeder-section"),

        html.Hr(className="cc-divider"),

        # BESCOM / KPTCL two-column grid
        html.Div([
            html.Div([
                html.Div("🔵 BESCOM",                    className="cc-agency-title"),
                html.Div(f"{bescom['events']:,}",         className="cc-agency-value"),
                html.Div("events",                       className="cc-agency-sub"),
                html.Div(_fmt(bescom['total_mins']),      className="cc-agency-value"),
                html.Div("outage mins",                  className="cc-agency-sub"),
                html.Div(f"CMI {_fmt(bescom['total_cmi'])}", className="cc-agency-cmi"),
            ], className="cc-agency-col"),

            html.Div(className="cc-agency-vr"),

            html.Div([
                html.Div("🟡 KPTCL",                    className="cc-agency-title"),
                html.Div(f"{kptcl['events']:,}",          className="cc-agency-value"),
                html.Div("events",                      className="cc-agency-sub"),
                html.Div(_fmt(kptcl['total_mins']),       className="cc-agency-value"),
                html.Div("outage mins",                 className="cc-agency-sub"),
                html.Div(f"CMI {_fmt(kptcl['total_cmi'])}", className="cc-agency-cmi"),
            ], className="cc-agency-col"),

        ], className="cc-agency-grid"),

    ], className="cc-base-card cc-enriched-card")



# ── Plotly helpers ────────────────────────────────────────────────────────────

def _base_layout(**extra):
    base = dict(
        plot_bgcolor=_SURFACE,
        paper_bgcolor=_SURFACE,
        font=dict(size=12, color=_INK),
        legend=dict(orientation="h", y=1.12, x=0),
        hovermode="y unified",
        hoverlabel=dict(bgcolor=_NAVY, font_color="white", font_size=12,
                        bordercolor=_CYAN),
    )
    base.update(extra)
    return base
    
def _detect_anomalies(series: pd.Series, z_thresh: float = TREND_ANOMALY_ZSCORE):
    """Return boolean mask of anomaly positions."""
    if len(series) < 4:
        return pd.Series([False] * len(series), index=series.index)
    mu, sigma = series.mean(), series.std()
    if sigma == 0:
        return pd.Series([False] * len(series), index=series.index)
    return (series - mu) / sigma > z_thresh


'''def build_trend_fig(daily: pd.DataFrame, height=280) -> go.Figure:
    fig = go.Figure()
    if not daily.empty:
        fig.add_bar(
            x=daily["event_date"], y=daily["total_events"],
            name="Outages", marker_color=_PRIMARY, opacity=.75,
            hovertemplate="<b>%{x}</b><br>Outages: <b>%{y}</b><extra></extra>",
        )
        fig.add_scatter(
            x=daily["event_date"], y=daily["total_duration"],
            name="Duration (min)", yaxis="y2",
            line=dict(color=_DANGER, width=2.5),
            mode="lines+markers",
            marker=dict(size=5, color=_DANGER),
            hovertemplate="Duration: <b>%{y:.1f} min</b><extra></extra>",
        )
    fig.update_layout(
        **_base_layout(
            height=height,
            margin=dict(t=30, b=40, l=52, r=60),
            hovermode="x unified",
            yaxis=dict(title="Outage count", color=_MUTED,
                       gridcolor=_DIVIDER, gridwidth=1),
            yaxis2=dict(title="Duration (min)", overlaying="y", side="right",
                        color=_DANGER, showgrid=False),
            bargap=0.3,
        )
    )
    return fig'''
    
def build_trend_fig(daily: pd.DataFrame, height=300, granularity: str = "day") -> go.Figure:
    """
    Three-trace trend chart:
      - Bar:        Outage count  (y1, left)
      - Line:       Duration mins (y2, right)
      - Shaded area: CMI          (y3, far-right, optional)
    Auto-annotates anomaly spikes above TREND_ANOMALY_ZSCORE std-devs.
    Handles day / week / month granularity labels.
    """
    fig = go.Figure()
    if daily.empty:
        fig.update_layout(**_base_layout(height=height,
            margin=dict(t=30, b=40, l=52, r=80)))
        return fig

    x = daily["period"] if "period" in daily.columns else daily["event_date"]
    has_cmi = "total_cmi" in daily.columns and daily["total_cmi"].sum() > 0

    # ── Trace 1: outage count bars ──────────────────────────────────────
    fig.add_bar(
        x=x, y=daily["total_events"],
        name="Outages", marker_color=_PRIMARY, opacity=0.78,
        yaxis="y",
        hovertemplate="%{x}<br>Outages: <b>%{y}</b><extra></extra>",
    )

    # ── Trace 2: duration line ──────────────────────────────────────────
    fig.add_scatter(
        x=x, y=daily["total_duration"],
        name="Duration (min)", yaxis="y2",
        line=dict(color=_DANGER, width=2.5),
        mode="lines+markers",
        marker=dict(size=5, color=_DANGER),
        hovertemplate="Duration: <b>%{y:.1f} min</b><extra></extra>",
    )

    # ── Trace 3: CMI shaded area ────────────────────────────────────────
    if has_cmi:
        fig.add_scatter(
            x=x, y=daily["total_cmi"],
            name="CMI", yaxis="y3",
            fill="tozeroy",
            fillcolor="rgba(123,63,160,0.10)",
            line=dict(color="rgba(123,63,160,0.55)", width=1.5, dash="dot"),
            mode="lines",
            hovertemplate="CMI: <b>%{y:,.0f}</b><extra></extra>",
        )

    # ── Anomaly annotations ─────────────────────────────────────────────
    if not daily.empty and len(daily) >= 4:
        mask = _detect_anomalies(daily["total_events"])
        for idx in daily[mask].index:
            val = daily.loc[idx, "total_events"]
            xval = x.iloc[list(daily.index).index(idx)] if hasattr(x, "iloc") else x[idx]
            fig.add_annotation(
                x=xval, y=val, yref="y",
                text=f"peak: {int(val)}",
                showarrow=True, arrowhead=2, arrowcolor=_WARNING,
                font=dict(size=10, color=_WARNING, family="monospace"),
                bgcolor="rgba(255,255,255,0.85)",
                bordercolor=_WARNING, borderwidth=1,
                borderpad=3, ay=-32, ax=0,
            )

    # ── Layout ──────────────────────────────────────────────────────────
    y3_axis = dict(
        title="CMI", overlaying="y", side="right",
        position=1.0, color="#7b3fa0", showgrid=False,
        tickformat=",", anchor="free",
    ) if has_cmi else dict(visible=False)

    gran_label = {"day": "Date", "week": "Week", "month": "Month"}.get(granularity, "Date")

    fig.update_layout(
        **_base_layout(
            height=height,
            margin=dict(t=36, b=44, l=56, r=90 if has_cmi else 60),
            hovermode="x unified",
            bargap=0.3,
            yaxis=dict(title="Outage count", color=_MUTED,
                       gridcolor=_DIVIDER, gridwidth=1),
            yaxis2=dict(title="Duration (min)", overlaying="y", side="right",
                        color=_DANGER, showgrid=False),
            yaxis3=y3_axis,
            xaxis=dict(title=gran_label),
            legend=dict(orientation="h", y=1.10, x=0, font=dict(size=11)),
        )
    )
    return fig


'''def build_station_fig(sdf: pd.DataFrame, height=420) -> go.Figure:
    """
    Improved station-level horizontal bar chart:
    - Colour-coded by events count (sequential scale)
    - Rich hover: Station | Events | Total mins | Avg mins
    - Annotation for top bar
    - Readable left-axis labels (station only, division as custom data)
    """
    fig = go.Figure()
    if sdf.empty:
        return fig.update_layout(
            **_base_layout(height=height,
                           margin=dict(t=10, b=40, l=10, r=20))
        )

    df = sdf.head(20).copy().sort_values("total_mins")
    colors = px.colors.sequential.Blues[2:]
    n = len(df)
    bar_colors = [colors[int(i / max(n-1, 1) * (len(colors)-1))] for i in range(n)]

    fig.add_bar(
        y=df["station"],
        x=df["total_mins"],
        orientation="h",
        marker=dict(
            color=df["total_mins"],
            colorscale="Blues",
            showscale=True,
            colorbar=dict(
                title=dict(text="Outage<br>Minutes", font=dict(size=11)),
                thickness=12, len=0.6,
                tickfont=dict(size=10),
            ),
            line=dict(color="rgba(255,255,255,.3)", width=0.5),
        ),
        customdata=df[["division", "events", "avg_mins"]].values,
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Division: <b>%{customdata[0]}</b><br>"
            "Events: <b>%{customdata[1]}</b><br>"
            "Total: <b>%{x:,.1f} min</b><br>"
            "Avg/event: <b>%{customdata[2]:.1f} min</b>"
            "<extra></extra>"
        ),
        text=df["events"].astype(str) + " ev",
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", size=11),
    )

    fig.update_layout(
        **_base_layout(
            height=height,
            hovermode="y",
            margin=dict(t=20, b=50, l=140, r=100),
            xaxis=dict(
                title="Total Outage Minutes",
                gridcolor=_DIVIDER,
                gridwidth=1,
                tickformat=",",
            ),
            yaxis=dict(
                tickfont=dict(size=12),
                autorange="reversed",
            ),
        )
    )
    return fig'''
    
def build_station_fig(
    sdf: pd.DataFrame,
    height: int = 420,
    metric: str = "total_mins",     # "total_mins" | "events" | "cmi" | "avg_mins"
    color_by_division: bool = True,
) -> go.Figure:
    """
    Station-level horizontal bar chart with:
      - Metric switcher:     total_mins / events / cmi / avg_mins
      - Color by division:   each division gets a distinct categorical colour
      - Rich hover tooltip
    """
    fig = go.Figure()
    if sdf.empty:
        return fig.update_layout(**_base_layout(height=height,
            margin=dict(t=10, b=40, l=10, r=20)))

    _metric_map = {
        "total_mins": ("Total Outage Minutes", "total_mins", ","),
        "events":     ("Event Count",          "events",     ",d"),
        "cmi":        ("CMI",                  "cmi",        ",.0f"),
        "avg_mins":   ("Avg Duration (min)",   "avg_mins",   ".1f"),
    }
    x_title, x_col, x_fmt = _metric_map.get(metric, _metric_map["total_mins"])

    # fill missing columns gracefully
    for col in ("cmi", "avg_mins"):
        if col not in sdf.columns:
            sdf = sdf.copy()
            sdf[col] = 0

    df = sdf.head(20).copy().sort_values(x_col, ascending=True)

    # Division colour mapping
    divs = df["division"].unique().tolist()
    div_color = {d: _DIV_PALETTE[i % len(_DIV_PALETTE)] for i, d in enumerate(divs)}
    bar_colors = df["division"].map(div_color).tolist() if color_by_division else None

    # Legend traces — one invisible scatter per division (for legend entries)
    if color_by_division:
        for div, col in div_color.items():
            fig.add_scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=col, size=10, symbol="square"),
                name=div, showlegend=True,
            )

    fig.add_bar(
        y=df["station"],
        x=df[x_col],
        orientation="h",
        marker_color=bar_colors if color_by_division else df[x_col],
        marker_showscale=not color_by_division,
        marker_line=dict(color="rgba(255,255,255,.25)", width=0.5),
        # ↑ Use flat marker_ kwargs — avoids the nested dict conflict entirely
        customdata=df[["division", "events", "total_mins", "avg_mins", "cmi"]].values,
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Division: %{customdata[0]}<br>"
            "Events: %{customdata[1]:,}<br>"
            "Total mins: %{customdata[2]:,.1f}<br>"
            "Avg/event: %{customdata[3]:.1f} min<br>"
            "CMI: %{customdata[4]:,.0f}"
            "<extra></extra>"
        ),
        text=df[x_col].apply(lambda v: f"{v:,.0f}"),
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", size=10),
        showlegend=False,
        name="",
    )

    fig.update_layout(
        **_base_layout(
            height=height,
            hovermode="y",
            margin=dict(t=24, b=50, l=150, r=60),
            xaxis=dict(title=x_title, gridcolor=_DIVIDER, gridwidth=1, tickformat=","),
            yaxis=dict(tickfont=dict(size=11), autorange="reversed"),
            legend=dict(orientation="v", x=1.01, y=1, font=dict(size=10)),
            bargap=0.25,
        )
    )
    return fig


def build_hbar_fig(df: pd.DataFrame, x_col: str, y_col: str,
                   text_col: str = None, height=320, color=None) -> go.Figure:
    fig = go.Figure()
    if not df.empty:
        fig.add_bar(
            y=df[y_col], x=df[x_col],
            orientation="h",
            marker_color=color or _PRIMARY,
            opacity=.85,
            text=(df[text_col].astype(str) if text_col and text_col in df.columns else None),
            textposition="auto",
            hovertemplate="<b>%{y}</b><br>%{x:,.1f} min<extra></extra>",
        )
    fig.update_layout(
        **_base_layout(
            height=height,
            margin=dict(t=10, b=40, l=10, r=20),
            xaxis=dict(title="Total outage minutes", gridcolor=_DIVIDER),
            yaxis=dict(autorange="reversed"),
        )
    )
    return fig


# ── TAB 1: Overview ───────────────────────────────────────────────────────────
def build_overview():
    return dbc.Container([
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.Span("Level scope", className="toggle-label"),
                        dbc.RadioItems(
                            id="overview-level-mode",
                            options=[
                                {"label": "Feeder only", "value": "feeder"},
                                {"label": "Equipment only", "value": "equipment"},
                                {"label": "Feeder + Equipment", "value": "both"},
                                ],
                            value="feeder",
                            inline=True,
                            className="overview-level-mode",
                            input_checked_style={"backgroundColor": "#0098d4", "borderColor": "#0098d4"},
                            ),
                        ], className="overview-toggle-wrap")
                    ], width=12)
                ], className="mb-3", id="overview-level-mode-wrap"),
        dbc.Row([
            dbc.Col(
                section_heading("⏱️", "KEY PERFORMANCE INDICATORS"),
                width=12
            ),
        ], className="mb-2"),
        dbc.Row(id="overview-kpi-row", className="g-3 mb-4"),
        dbc.Row([
            dbc.Col(
                section_heading("⚡", "GRID RELIABILITY INDICES"),
                width=12
            ),
        ], className="mb-2"),
        dbc.Row(id="overview-reliability-row", className="g-3 mb-4"),
        dbc.Row([
            dbc.Col(
                section_heading("📡 ", "Zone Monitored Base"),
                width=12
            ),
        ], className="mb-2"),
        dbc.Row(id="overview-cc-cards", className="g-3 mb-4"),
        # ROW 2 — Daily trend full width
        dbc.Row([
            dbc.Col(
                section_heading("📉 ", "Daily Trend — Outages & Duration"),
                width=12
            ),
        ], className="mb-2"),
        dbc.Row([
            dbc.Col([
                html.Div(
                    dcc.Graph(id="overview-trend-chart",
                              config={"displayModeBar": False}),
                    className="chart-card chart-card--full",
                ),
            ], width=12),
        ], className="g-3 mb-4"),
        dbc.Row([
            dbc.Col([
                section_heading("🏆 ", "Top Affected Subdivisions"),
                html.Div(dcc.Graph(id="overview-top-chart",
                                   config={"displayModeBar": False}),
                         className="chart-card"),
            ], md=7),
            dbc.Col([
                section_heading("🚨", "Action Priority Queue"),
                html.Div(id="overview-alerts"),
            ], lg=5, md=12),
        ]),
    ], fluid=True, className="tab-pane")

# ── TAB 2 & 3: CC-specific ────────────────────────────────────────────────────
def build_explorer_tab():
    sfx = "ex"

    # Static column list for modals (populated dynamically via callback but need initial)
    _default_cols = ["Date","Trouble Time", "Division","Sub-Division","Station","Feeder","Type","Status","Agency","Equipment / Feeder","Duration (min)","Cause"]

    return dbc.Container([
        dbc.Row([dbc.Col([
            html.H5("⚡ Interruption Explorer",
                    className="page-title"),
            html.P("Select Zone(s) then cascade: Division → Station → Feeder(s). Leave blank for all levels below.",
                   className="page-subtitle text-muted"),
        ])]),

        html.Div([
            hierarchy_dropdowns(
                f"dd-cc-{sfx}", f"dd-div-{sfx}",
                f"dd-stn-{sfx}", f"dd-fdr-{sfx}",
                default_cc=None,
            ),
            dbc.Row([
                dbc.Col([
                    html.Label("Outage Type"),
                    dcc.Dropdown(id=f"dd-otype-{sfx}",
                                 options=[{"label": t, "value": t}
                                          for t in ["Scheduled","Unscheduled"]],
                                 placeholder="All types", clearable=True),
                ], xs=12, sm=4, md=2),
                dbc.Col([
                    html.Label("Utility"),
                    dcc.Dropdown(id=f"dd-agency-{sfx}",
                                 options=[{"label": a, "value": a}
                                          for a in ["BESCOM","KPTCL"]],
                                 placeholder="KPTCL + BESCOM", clearable=True),
                ], xs=12, sm=4, md=2),
                dbc.Col([
                    dbc.Button("🔍 Apply Filters", id=f"btn-apply-{sfx}",
                               className="btn-apply mt-4"),
                ], xs=12, sm=4, md=2, className="d-flex align-items-end"),
            ], className="g-3 mb-2"),
        ], className="filter-bar"),

        html.Div(id=f"breadcrumb-{sfx}", className="breadcrumb-strip",
                 children="📍 All networks"),
        dbc.Row([
                    dbc.Col(section_heading("⏱️", "KEY PERFORMANCE INDICATORS"), width=12),
                ], className="mb-2"),
        dbc.Row(id=f"kpi-row-{sfx}",
            children=[
                dbc.Col(
                    html.Div("Select filters and click Apply, or switch to this tab to load data.",
                        className="text-muted small py-3"),
                    width=12
                )
            ], className="g-3 mb-4"),

        dbc.Row([
            dbc.Col(section_heading("⚡", "GRID RELIABILITY INDICES"), width=12),
        ], className="mb-2"),
                dbc.Row(id=f"reliability-row-{sfx}",
                    children=[
                        dbc.Col(
                            html.Div("Reliability indices load after Apply.",
                                className="text-muted small py-3"),
                            width=12
                        )
                    ], className="g-3 mb-4"),
                    
        dbc.Row([
            dbc.Col([
                html.Div([
                    dbc.Button("📈 Daily Trend", id=f"subtab-trend-btn-{sfx}",
                        className="btn-subtab active", n_clicks=0),
                    dbc.Button("🏭 Station Summary", id=f"subtab-stn-btn-{sfx}",
                        className="btn-subtab", n_clicks=0),
                ], className="subtab-nav mb-2"),

                dcc.Store(id=f"subtab-charts-{sfx}", data="trend"),

                # ── Pane 1: Trend ──────────────────────────────
                html.Div([
                    section_heading("📈", "Daily Outage Trend"),
                    html.Div([
                        dbc.RadioItems(
                            id=f"trend-gran-{sfx}",
                            options=[
                                {"label": "Day", "value": "day"},
                                {"label": "Week", "value": "week"},
                                {"label": "Month", "value": "month"},
                            ],
                            value="day", inline=True,
                            className="radio-inline-lite mb-2",
                            input_checked_style={"backgroundColor": _PRIMARY, "borderColor": _PRIMARY},
                        ),
                    ], className="mb-1"),
                    html.Div(
                        dcc.Graph(id=f"chart-trend-{sfx}", config={"displayModeBar": False}),
                        className="chart-card",
                    ),
                ], id=f"chart-pane-trend-{sfx}", style={"display": "block"}),

                # ── Pane 2: Station summary ─────────────────────
                html.Div([
                    section_heading("🏭", "Station-level Outage Summary"),
                    html.Div([
                        dbc.RadioItems(
                            id=f"station-metric-{sfx}",
                            options=[
                                {"label": "⏱ Total Mins", "value": "total_mins"},
                                {"label": "⚡ Event Count", "value": "events"},
                                {"label": "📉 CMI", "value": "cmi"},
                                {"label": "⌛ Avg Duration", "value": "avg_mins"},
                            ],
                            value="total_mins", inline=True,
                            className="radio-inline-lite mb-2",
                            input_checked_style={"backgroundColor": _PRIMARY, "borderColor": _PRIMARY},
                        ),
                    ], className="d-flex flex-wrap align-items-center gap-3 mb-2"),
                    html.Div(
                        dcc.Graph(
                            id=f"chart-stn-{sfx}",
                            config={
                                "displayModeBar": "hover",
                                "toImageButtonOptions": {
                                    "format": "png", "filename": f"station_chart_{sfx}", "scale": 2,
                                },
                            },
                        ),
                        className="station-chart-card",
                        id=f"station-chart-wrap-{sfx}",
                    ),
                ], id=f"chart-pane-stn-{sfx}", style={"display": "none"}),

            ], width=12),
        ], className="mb-4"),
        
        section_heading("📋", "Interruption Event Detail"),

        # Hidden stores for table data, chosen columns, selected rows, col filters, sort
        dcc.Store(id=f"store-tbl-full-{sfx}"),
        dcc.Store(id=f"store-tbl-{sfx}"),
        dcc.Store(id=f"store-cols-{sfx}", data=_default_cols),
        dcc.Store(id=f"store-selected-{sfx}", data=[]),
        dcc.Store(id=f"store-col-filters-{sfx}", data={}),
        dcc.Store(id=f"store-sort-{sfx}", data=[]),

        # ── Per-column searchable dropdown filter row ─────────────────
        html.Div(id=f"filter-row-{sfx}", className="dt-filter-row"),

        html.Div(id=f"table-limit-note-{sfx}", className="table-limit-note"),

        # Table wrapper (static DataTable instance + toolbar)
        html.Div([
            make_table_toolbar(sfx),
            dash_table.DataTable(
                id=f"datatable-{sfx}",
                data=[],
                columns=[],
                **_TABLE_BASE,
            ),
        ], className="bicc-table-wrapper mb-4"),

        # Modals
        make_col_modal(sfx, _default_cols),
        make_chart_modal(sfx),

    ], fluid=True, className="tab-pane")
    
    # CORRECT
def _unique_feeder_customers(df: pd.DataFrame) -> int:
    if "Customers Affected" not in df.columns or "Feeder" not in df.columns:
        return 0
    return int(df.groupby("Feeder")["Customers Affected"].max().sum())
    
# ── ADMIN TAB ─────────────────────────────────────────────────────────────────
# Required columns for upload validation
_REQUIRED_UPLOAD_RULES = {
    "feeder": lambda c: c == "feeder" or ("feeder" in c and "type" not in c),
    "trouble_dt": lambda c: ("trouble" in c) or (c == "trouble_dt"),
}

def _validate_upload_df(df: pd.DataFrame):
    """
    Returns (ok: bool, errors: list[str], warnings: list[str]).
    Accepts raw Excel headers such as 'Trouble Date/time' from the sample file.
    """
    errors, warnings = [], []
    if df is None or df.empty:
        return False, ["Uploaded file has no data rows."], []

    cols_lower = [str(c).strip().lower() for c in df.columns]

    missing = []
    for req, rule in _REQUIRED_UPLOAD_RULES.items():
        if not any(rule(c) for c in cols_lower):
            missing.append(req)
    if missing:
        errors.append(f"Missing required column(s): {', '.join(missing)}")

    valid_rows = df.dropna(how="all")
    if len(valid_rows) == 0:
        errors.append("All rows are empty after dropping blank lines.")

    if not errors and not any(_REQUIRED_UPLOAD_RULES["trouble_dt"](c) for c in cols_lower):
        warnings.append("Cannot derive event_date — trouble date/time column not found.")

    return len(errors) == 0, errors, warnings

# ============================================================
# NEW: Trends & Comparison tab — app.py additions
# Suffix convention: "tr" (mirrors "ex" for Explorer, "ov" implicit for Overview)
# Purely additive — no existing function, callback, or ID is modified.
# ============================================================

# ---- 1. Layout builder -------------------------------------------------

def build_trends_tab():
    sfx = "tr"
    return dbc.Container([
        dbc.Row(dbc.Col([
            html.H5("Trends & Comparison", className="page-title"),
            html.P("Compare periods, track reliability trends, and spot repeat-offender feeders.",
                   className="page-subtitle text-muted"),
        ])),

        # Shared filter bar — same structure as Explorer, id suffix "tr"
        html.Div([
            hierarchy_dropdowns(f"dd-cc-{sfx}", f"dd-div-{sfx}", f"dd-stn-{sfx}", f"dd-fdr-{sfx}", default_cc=['BICC-1', 'BICC-2']),
            dbc.Row([
                dbc.Col([
                    html.Label("Outage Type"),
                    dcc.Dropdown(
                        id=f"dd-otype-{sfx}",
                        options=[{"label": t, "value": t} for t in ["Scheduled", "Unscheduled"]],
                        placeholder="All types", clearable=True,
                    ),
                ], xs=12, sm=4, md=2),
                dbc.Col([
                    html.Label("Utility"),
                    dcc.Dropdown(
                        id=f"dd-agency-{sfx}",
                        options=[{"label": a, "value": a} for a in ["BESCOM", "KPTCL"]],
                        placeholder="KPTCL / BESCOM", clearable=True,
                    ),
                ], xs=12, sm=4, md=2),
                dbc.Col(dbc.Button("Apply Filters", id=f"btn-apply-{sfx}", className="btn-apply mt-4"),
                        xs=12, sm=4, md=2),
            ], className="d-flex align-items-end g-3 mb-2"),
        ], className="filter-bar"),

        html.Div(id=f"breadcrumb-{sfx}", className="breadcrumb-strip", children="All networks"),
    # ── NEW: Subtab nav ──────────────────────────────────────────────
    html.Div([
        dbc.Button("Period Comparison", id=f"subtab-compare-btn-{sfx}",
                   className="btn-subtab active", n_clicks=0),
        dbc.Button("Reliability Trends & Repeat Offenders", id=f"subtab-reliability-btn-{sfx}",
                   className="btn-subtab", n_clicks=0),
        ], className="subtab-nav mb-3"),
    dcc.Store(id=f"store-subtab-{sfx}", data="compare"),
        # ---- Section 1: Period Comparison ----
        html.Div([
        html.Div([
            html.Div([
                html.Div("Period Comparison", className="trends-section-title"),
                html.Div([
                    html.Span("Period A", className="period-label"),
                    dcc.DatePickerRange(id=f"period-a-{sfx}", display_format="DD MMM YYYY",
                                         className="period-picker"),
                    html.Span("VS", className="period-vs-badge"),
                    html.Span("Period B", className="period-label"),
                    dcc.DatePickerRange(id=f"period-b-{sfx}", display_format="DD MMM YYYY",
                                         className="period-picker"),
                    dbc.Button("Compare", id=f"btn-compare-{sfx}", className="btn-apply"),
                ], className="period-toolbar-group"),
            ], className="section-toolbar"),
        ], className="trends-section mb-3"),
       dbc.Row(id=f"compare-kpi-row-{sfx}", className="g-3 mb-3"),
       dbc.Row(
            dbc.Col(
                html.Div([
                    html.Div("Daily Outage Overlay — Period A vs Period B",
                              className="chart-card-title"),
                    dcc.Graph(id=f"chart-overlay-{sfx}", config={"displayModeBar": False}),
                ], className="chart-card"),
                width=12,
            ),
            className="g-3 mb-4"),
    ], id=f"pane-compare-{sfx}", style={"display": "block"}),
      # ── PANE 2: Reliability Trends & Repeat Offenders ────────────────
    html.Div([
        # ---- Section 2: Reliability Trends + Zone Ranking ----
        html.Div([
        html.Div([
        html.Div("Reliability Trends & Division Ranking", className="trends-section-title"),
        dbc.RadioItems(
            id=f"reliability-view-{sfx}",
            options=[
                {"label": "Top Drivers", "value": "topn"},
                {"label": "Full Zone Heatmap", "value": "heatmap"},
            ],
            value="topn", inline=True,
            className="radio-inline-lite mb-0 me-3",
            inputCheckedStyle={"backgroundColor": _PRIMARY, "borderColor": _PRIMARY},
        ),
        dbc.RadioItems(
            id=f"trend-gran-{sfx}",
            options=[{"label": "Month", "value": "month"}, {"label": "Quarter", "value": "quarter"}],
            value="month", inline=True, className="radio-inline-lite mb-0"
                ),
            ], className="section-toolbar"),
        ], className="trends-section mb-3"),
       dbc.Row([
            dbc.Col(html.Div([
                html.Div("SAIDI Trend (min/customer)", className="chart-card-title"),
                dcc.Graph(id=f"chart-saidi-{sfx}", config={"displayModeBar": False}),
            ], className="chart-card"), md=6),
            dbc.Col(html.Div([
                html.Div("SAIFI Trend (interruptions/customer)", className="chart-card-title"),
                dcc.Graph(id=f"chart-saifi-{sfx}", config={"displayModeBar": False}),
            ], className="chart-card"), md=6),
        ], className="g-3 mb-4"),
        dbc.Row(dbc.Col(html.Div(id=f"zone-rank-{sfx}", className="chart-card"), width=12), className="g-3 mb-4"),
        # ---- Section 3: Repeat Offenders ----
        html.Div([
        html.Div([
        html.Div("Repeat Tripped Feeders — Monthly Breakdown", className="trends-section-title"),
        html.Div([
            html.Label("Min events (period)", className="form-label-lite me-1 mb-0"),
            dcc.Input(id=f"min-events-{sfx}", type="number", value=10, min=1, step=1,
                       className="min-events-input", debounce=True),
                html.Label([
                    dcc.Checklist(
                        id=f"split-month-{sfx}",
                        options=[{"label": " Split by month", "value": "split"}],
                        value=["split"],
                        className="split-month-check",
                        inputClassName="split-month-checkbox",
                    ),
                ], className="split-month-label"),
                dbc.Button("Export", id=f"btn-export-offenders-{sfx}",
                           className="btn-toolbar-action"),
                dcc.Download(id=f"download-offenders-{sfx}"),
                ], className="offenders-toolbar-group"),
            ], className="section-toolbar"),
            ], className="trends-section mb-3"),
        dbc.Row(dbc.Col(html.Div(id=f"offenders-table-{sfx}", className="chart-card"), width=12), className="g-3 mb-4"),
        ], id=f"pane-reliability-{sfx}", style={"display": "none"}),

        dcc.Store(id=f"store-compare-{sfx}", data=None),
        dcc.Store(id=f"store-trend-{sfx}", data=None),
        dcc.Store(id=f"store-offenders-{sfx}", data=None),
    ], fluid=True, className="tab-pane")


def build_admin_tab():
    return dbc.Container([
        dbc.Row([dbc.Col([
            html.H5("🛠 Admin — Data Upload Console", className="page-title"),
            html.P("Authenticated admins can upload new interruption data (XLSX) incrementally.",
                   className="page-subtitle text-muted"),
        ])]),

        # ── Access-denied panel (shown when SSO group check fails) ─────
        html.Div(id="admin-login-panel", children=[
            dbc.Card([
                dbc.CardBody([
                    html.H6("🔒 Admin Access Restricted", className="mb-2"),
                    html.Div(id="admin-login-msg", className="text-muted small"),
                ])
            ], className="mb-4 shadow-sm"),
        ]),

        # ── Console (hidden until login) ──────────────────────────────
        html.Div(id="admin-console", style={"display": "none"}, children=[

            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H6("📥 Download Sample Format", className="mb-2"),
                            html.P("Use this as the template for new uploads.",
                                   className="text-muted small mb-3"),
                            dbc.Button("⬇ Download Sample XLSX",
                                       id="btn-download-sample",
                                       color="secondary", outline=True, size="sm"),
                            dcc.Download(id="download-sample-xlsx"),
                        ])
                    ], className="h-100 shadow-sm"),
                ], md=4),

                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H6("📤 Upload New Data (XLSX)", className="mb-2"),
                            html.P("Accepts .xlsx files. Duplicates are detected via "
                                   "composite key (event_date | feeder | trouble_dt).",
                                   className="text-muted small mb-2"),
                            dcc.Upload(
                                id="upload-data",
                                children=html.Div([
                                    html.Div("📂 Drop XLSX here or click to browse", className="upload-title"),
                                    html.Div("Only .xlsx files are accepted", className="upload-subtitle"),
                                ], className="upload-drop-inner"),
                                className="upload-dropzone",
                                accept=".xlsx",
                                multiple=False,
                            ),
                        ])
                    ], className="h-100 shadow-sm"),
                ], md=8),
            ], className="mb-4"),

            # ── Upload status / summary ───────────────────────────────
            html.Div(id="admin-upload-status"),

            # ── DB stats ─────────────────────────────────────────────
            html.Div([
                html.H6("📊 Current Database Stats", className="mt-4 mb-2"),
                html.Div(id="admin-db-stats"),
            ]),

            # ── Rollback Manager ─────────────────────────────────────
            html.Hr(className="my-4"),
            dbc.Row([dbc.Col([
                html.H6("↩ Rollback Manager", className="mb-1"),
                html.P(
                    "The last 3 successful delta uploads are listed below. "
                    "Click Rollback to permanently remove that batch from the database.",
                    className="text-muted small mb-3",
                ),
            ])]),

            # Upload cards populated by callback
            html.Div(id="rollback-cards", className="mb-3"),

            # Pending upload_id store
            dcc.Store(id="rollback-pending-id", data=None),

            # Confirmation modal
            dbc.Modal([
                dbc.ModalHeader(
                    dbc.ModalTitle("⚠️ Confirm Rollback"),
                    close_button=False,
                ),
                dbc.ModalBody(id="rollback-modal-body"),
                dbc.ModalFooter([
                    dbc.Button("Cancel",        id="rollback-cancel-btn",
                               color="secondary", outline=True, className="me-2"),
                    dbc.Button("Yes, Rollback", id="rollback-confirm-btn",
                               color="danger"),
                ]),
            ], id="rollback-confirm-modal", is_open=False,
               centered=True, backdrop="static"),

            # Result alert
            html.Div(id="rollback-result"),

        ]),

        dcc.Store(id="store-admin-auth", data=False),
        dcc.Store(id="store-db-stats"),

    ], fluid=True, className="tab-pane")

#Landing Page Layout Changes
def build_landing():
    """
    Landing page with four nav cards.
    Clicking a card triggers the page-state store to switch sections.
    No tabs; no dbc.Tabs.  All existing content builders reused unchanged.
    """
    cards = [
        {
            "icon": "📊",
            "title": "Overview",
            "desc": "Network-wide KPIs, reliability indices, zone health, and outage trends.",
            "tab": "tab-overview",
        },
        {
            "icon": "⚡",
            "title": "Interruption Explorer",
            "desc": "Explore interruptions across zones — select one or both zones with full cascade filters.",
            "tab": "tab-ex",
        },
        {
            "icon": "...", 
            "title": "Trends & Comparison",
            "desc": "Compare periods, track SAIDI/SAIFI trends, and spot repeat-offender feeders.",
            "tab": "tab-trends"
        },
        {
            "icon": "🛠",
            "title": "Admin",
            "desc": "Upload new interruption data, manage uploads, and rollback batches.",
            "tab": "tab-admin",
        },
    ]

    card_elements = []
    for c in cards:
        card_elements.append(
            html.Div(
                [
                    html.Div(c["icon"], className="nav-card__icon"),
                    html.P(c["title"], className="nav-card__title"),
                    html.P(c["desc"], className="nav-card__desc"),
                    html.Span("Open →", className="nav-card__arrow"),
                ],
                className="nav-card",
                id=f"nav-card-{c['tab']}",
                n_clicks=0,
                tabIndex=0,
                role="button",
                **{"aria-label": f"Open {c['title']}"},
            )
        )

    return html.Div(
        [
            # Hero band
            html.Div(
                [
                    html.H1("BESCOM BICC Dashboard", className="landing-hero__title"),
                    html.P(
                        "Select a section to begin. Use the date picker above to set the reporting period.",
                        className="landing-hero__sub",
                    ),
                ],
                className="landing-hero",
            ),
            # Cards grid
            html.Div(card_elements, className="landing-cards"),
        ],
        id="landing-page",
    )
#Landing Page Layout Changes ENDS
# ── Global date range ─────────────────────────────────────────────────────────
_min_dt, _max_dt = get_date_range()
_default_start, _default_end = _latest_month_window()

# ── App layout ────────────────────────────────────────────────────────────────

#Landing Page Layout Changes STARTS
HEADER = dbc.Navbar(
    dbc.Container([
        # ── Brand ──────────────────────────────────────────────────────────
        html.Div([
            html.Img(src=BESCOM_LOGO, className="bescom-logo", alt="BESCOM"),
            html.Div([
                html.Span("BESCOM BICC", className="header-title"),
                html.Span(
                    "Bangalore Integrated Control Centre — General Manager's Dashboard",
                    className="header-subtitle",
                ),
            ], className="header-title-block"),
        ], className="header-brand"),

        # ── Date picker aligned to header band ─────────────────────────────
        html.Div([
            html.Span("📅 Period", className="header-date-label"),
            dcc.DatePickerRange(
                id="global-date-range",
                min_date_allowed=_min_dt,
                max_date_allowed=_max_dt,
                start_date=_default_start,
                end_date=_default_end,
                display_format="DD MMM YYYY",
                month_format="MMMM YYYY",
                first_day_of_week=1,
                updatemode="bothdates",
                className="date-range-lite",
            ),
            html.Div(
                f"Overview window limited to last {MAX_OVERVIEW_DAYS} days for performance.",
                className="date-range-note",
            ),
        ], className="header-date-zone"),
    ], fluid=True),
    color=_NAVY,
    dark=True,
    className="bicc-header",
)
app.layout = html.Div([
    HEADER,

    # Global stores (IDs unchanged — all callbacks stay wired)
    dcc.Store(id="store-date-range",  data={"start": _default_start, "end": _default_end}),
    dcc.Store(id="store-level-mode",  data="feeder"),
    dcc.Store(id="store-data-version", data=0),
    dcc.Store(id="store-active-tab",  data="landing"),   # NEW: drives section visibility

    # Init interval for date picker restore (kept as-is)
    dcc.Interval(id="init-date-interval", interval=200, max_intervals=1),

    # ── Page regions ──────────────────────────────────────────────────────
    # Landing (shown by default)
    html.Div(build_landing(), id="region-landing"),

    # Back navigation strip (hidden on landing)
    html.Div(
        [
            html.Span("←", className="back-strip__icon"),
            html.Span("Back to Dashboard", className="back-strip__label"),
            html.Span(id="back-strip-section", className="back-strip__section"),
        ],
        id="back-strip",
        className="back-strip",
        n_clicks=0,
        style={"display": "none"},
    ),

    # Section panes — pre-built once, shown/hidden by CSS display
    html.Div(build_overview(),     id="region-overview", className="section-pane", style={"display": "none"}),
    html.Div(build_explorer_tab(), id="region-ex",        className="section-pane", style={"display": "none"}),
    html.Div(build_trends_tab(),   id="region-trends", className="section-pane", style={"display": "none"}),
    html.Div(build_admin_tab(),    id="region-admin",   className="section-pane", style={"display": "none"}),

    # Hidden dbc.Tabs stub — keeps existing `tabs` callbacks happy
    # activetab is driven by store-active-tab callback below
    dbc.Tabs(
        id="tabs",
        active_tab="tab-overview",
        style={"display": "none"},
        children=[
            dbc.Tab(tab_id="tab-overview"),
            dbc.Tab(tab_id="tab-ex"),
            dbc.Tab(tab_id="tab-trends"),   # <-- NEW
            dbc.Tab(tab_id="tab-admin"),
        ],
    ),
])
#Landing Page Layout Changes ENDS
# ── Global callbacks ──────────────────────────────────────────────────────────

@app.callback(
    Output("global-date-range", "start_date",       allow_duplicate=True),
    Output("global-date-range", "end_date",         allow_duplicate=True),
    Output("global-date-range", "min_date_allowed", allow_duplicate=True),
    Output("global-date-range", "max_date_allowed", allow_duplicate=True),
    Input("init-date-interval", "n_intervals"),
    prevent_initial_call="initial_duplicate",
)
def restore_date_picker(_n):
    """On every page load, set the picker to the true current DB bounds.
    Works correctly on cloud (no localStorage dependency)."""
    db_min, db_max = _get_db_date_bounds()
    return _default_start, _default_end, db_min, db_max


@app.callback(
    Output("store-date-range", "data"),
    Input("global-date-range", "start_date"),
    Input("global-date-range", "end_date"),
)
def sync_date_store(start, end):
    s, e = _clamp_date_window(start or _min_dt, end or _max_dt)
    return {"start": s, "end": e}
#Wiring changes STARTS
@app.callback(
    Output("period-a-tr", "min_date_allowed"),
    Output("period-a-tr", "max_date_allowed"),
    Output("period-a-tr", "start_date"),
    Output("period-a-tr", "end_date"),
    Input("store-date-range", "data"),
    State("period-a-tr", "start_date"),
    State("period-a-tr", "end_date"),
    prevent_initial_call=False,
)
def sync_period_a_with_global(date_store, a_start, a_end):
    ds = (date_store or {}).get("start") or _min_dt
    de = (date_store or {}).get("end") or _max_dt
    ds_ts, de_ts = pd.to_datetime(ds), pd.to_datetime(de)

    if not a_start or not a_end:
        span_days = max((de_ts - ds_ts).days + 1, 1)
        split_days = max(span_days // 2, 1)
        a_start_ts = ds_ts
        a_end_ts = min(ds_ts + pd.Timedelta(days=split_days - 1), de_ts)
        return ds, de, str(a_start_ts.date()), str(a_end_ts.date())

    a_start_ts, a_end_ts = pd.to_datetime(a_start), pd.to_datetime(a_end)
    if a_start_ts < ds_ts: a_start_ts = ds_ts
    if a_end_ts > de_ts: a_end_ts = de_ts
    if a_start_ts > a_end_ts:
        a_start_ts, a_end_ts = ds_ts, min(ds_ts, de_ts)
    return ds, de, str(a_start_ts.date()), str(a_end_ts.date())


@app.callback(
    Output("period-b-tr", "min_date_allowed"),
    Output("period-b-tr", "max_date_allowed"),
    Output("period-b-tr", "start_date"),
    Output("period-b-tr", "end_date"),
    Input("period-a-tr", "end_date"),
    Input("store-date-range", "data"),
    State("period-b-tr", "start_date"),
    State("period-b-tr", "end_date"),
    prevent_initial_call=False,
)
def sync_period_b_with_period_a(a_end, date_store, b_start, b_end):
    ds = (date_store or {}).get("start") or _min_dt
    de = (date_store or {}).get("end") or _max_dt
    ds_ts, de_ts = pd.to_datetime(ds), pd.to_datetime(de)

    min_b_ts = pd.to_datetime(a_end) if a_end else ds_ts
    if min_b_ts < ds_ts: min_b_ts = ds_ts
    if min_b_ts > de_ts: min_b_ts = de_ts

    if not b_start or not b_end:
        return str(min_b_ts.date()), de, str(min_b_ts.date()), str(de_ts.date())

    b_start_ts, b_end_ts = pd.to_datetime(b_start), pd.to_datetime(b_end)
    if b_start_ts < min_b_ts: b_start_ts = min_b_ts
    if b_end_ts > de_ts: b_end_ts = de_ts
    if b_start_ts > b_end_ts:
        b_start_ts, b_end_ts = min_b_ts, de_ts
    return str(min_b_ts.date()), de, str(b_start_ts.date()), str(b_end_ts.date())
@app.callback(
    Output("store-level-mode", "data"),
    Input("overview-level-mode", "value"),
)
#Wiring changes ENDS
def sync_level_mode(level_mode):
    return level_mode or "both"
    
@app.callback(
    Output("overview-kpi-row",     "children"),
    Output("overview-reliability-row", "children"),
    Output("overview-cc-cards",    "children"),
    Output("overview-trend-chart", "figure"),
    Output("overview-top-chart",   "figure"),
    Output("overview-alerts",      "children"),
    Input("store-date-range",   "data"),
    Input("store-level-mode", "data"),
    Input("store-data-version", "data"),
)
def update_overview(date_store, level_mode, _version):
    ds = date_store["start"]
    de = date_store["end"]
    kpi = get_overview_kpis(ds, de, level_mode=level_mode)
    daily = kpi["daily"]
    top = kpi["top_subs"]
    cc = kpi["cc_counts"]
    n_days = max(len(daily), 1)
    
    mode_label = {
        "feeder": "Feeder only",
        "equipment": "Equipment only",
        "both": "Feeder + Equipment",
    }.get(level_mode, "Feeder + Equipment")

    tot_ev = int(kpi["total_events"])
    tot_min = float(kpi["total_mins"])
    unique_customers = int(kpi["unique_customers"])
    tot_cust = int(kpi["total_customers"])
    cust_pct = (unique_customers / TOTAL_CUSTOMERS * 100) if TOTAL_CUSTOMERS > 0 else 0.0
    tot_cmi = float(kpi["total_cmi"])
    saidi = round(tot_cmi / TOTAL_CUSTOMERS, 4) if TOTAL_CUSTOMERS > 0 else 0.0
    saifi = round(tot_cust / TOTAL_CUSTOMERS, 4) if TOTAL_CUSTOMERS > 0 else 0.0 #SAIFI
    avg_ev  = round(tot_ev / n_days, 2) if n_days > 0 else 0.0
    #avg_min = round(tot_min / tot_cust, 2) if tot_cust > 0 else 0.0
    avg_min = tot_min / max(tot_ev, 1)

    kpi_row = [
        dbc.Col(kpi_card(
            "Total Outage Events", f"{tot_ev:,}", mode_label, "bolt",
            tooltip_lines=[
                f"Period: {ds} → {de}",
                f"Scope: {mode_label}",
                f"Days covered: {n_days}",
                f"Avg outages/day: {avg_ev}",
                f"Warn ≥ {KPI_EVENTS_WARN:,} | Critical ≥ {KPI_EVENTS_DANGER:,}",
            ], raw_value=tot_ev, warn=KPI_EVENTS_WARN, danger=KPI_EVENTS_DANGER,
        ), md=True),
        dbc.Col(kpi_card(
             "Total Minutes", f"{tot_min:,.1f}", "Outage duration", "warning",
             tooltip_lines=[
                        f"Avg per event: {avg_min:.1f} min", 
                        f"Scope: {mode_label}", 
                        f"Warn ≥ {KPI_MINS_WARN:,} | Critical ≥ {KPI_MINS_DANGER:,}"
                        ], raw_value=tot_min, warn=KPI_MINS_WARN, danger=KPI_MINS_DANGER,
            ), md=True),
        dbc.Col(kpi_card(
            "Unique Customers Affected",f"{unique_customers:,}", 
            f"of {TOTAL_CUSTOMERS:,} total · {cust_pct:.1f}%", 
            "people",
            tooltip_lines=[
                    f"Affected : {unique_customers:,}",
                    f"Total base : {TOTAL_CUSTOMERS:,}",
                    f"Impact : {cust_pct:.1f}% of consumer base",
                    f"Scope: {mode_label}",
                    f"Warn ≥ {KPI_CUST_WARN:,}, Critical ≥ {KPI_CUST_DANGER:,}",
                ],raw_value=unique_customers, warn=KPI_CUST_WARN, danger=KPI_CUST_DANGER,
        ), md=True),
        dbc.Col(kpi_card(
            "CMI", f"{tot_cmi:,.0f}", "Customer Minutes Interrupted", "cmi",
            tooltip_lines=[
                "CMI = Σ(customers_affected × duration_mins)",
                f"Scope: {mode_label}",
                f"Warn ≥ {KPI_CMI_WARN:,} | Critical ≥ {KPI_CMI_DANGER:,}",
            ], raw_value=tot_cmi, warn=KPI_CMI_WARN, danger=KPI_CMI_DANGER,
        ), md=True),
    ]
    # Grid Reliability Metrics
    reliability_row = [
            dbc.Col(kpi_card(
                "SAIDI",
                f"{saidi:.4f}",
                f"min/customer  •  CMI÷{TOTAL_CUSTOMERS:,}",
                "clock",
                tooltip_lines=[
                    "System Avg Interruption Duration Index",
                    f"Formula: CMI ÷ Total Customers",
                    f"CMI: {tot_cmi:,.0f} min",
                    f"Base: {TOTAL_CUSTOMERS:,} customers",
                    f"SAIDI: {saidi:.4f} min/customer",
                    f"Warn ≥ {KPI_SAIDI_WARN}, Critical ≥ {KPI_SAIDI_DANGER}",
                ],
                raw_value=saidi, warn=KPI_SAIDI_WARN, danger=KPI_SAIDI_DANGER,
            ), md=True),
            dbc.Col(kpi_card(
                "SAIFI",
                f"{saifi:.4f}",
                f"interruptions/customer  •  Customers Affected÷{TOTAL_CUSTOMERS:,}",
                "bolt",
                tooltip_lines=[
                    "System Avg Interruption Frequency Index",
                    f"Formula: Customers Affected ÷ Total Customers",
                    f"Customers Affected: {tot_cust:,}",
                    f"Base: {TOTAL_CUSTOMERS:,} customers",
                    f"SAIFI: {saifi:.4f} interruptions/customer",
                    f"Warn ≥ {KPI_SAIFI_WARN}, Critical ≥ {KPI_SAIFI_DANGER}",
                ],
                raw_value=saifi, warn=KPI_SAIFI_WARN, danger=KPI_SAIFI_DANGER,
            ), md=True),
        ]
        #agency_split        = kpi["agency_split"]
        #feeders_interrupted = kpi["feeders_interrupted"]
        #cc_cards = [
        #    dbc.Col(
        #        _cc_health_card(row, agency_split, feeders_interrupted),
        #        xs=12
        #    )
        #    for _, row in cc.iterrows()
        #]
    
    tot_ev  = kpi["total_events"]  or 1
    tot_min = kpi["total_mins"]    or 1
    tot_cmi = kpi["total_cmi"]     or 1

    def _bar(pct, color):
        return html.Div(
            html.Div(className="cc-metric-fill",
                     style={"width": f"{min(float(pct), 100):.1f}%",
                            "background": color}),
            className="cc-metric-track",
        )

    def _metric_row(label, value_str, pct, color):
        return html.Div([
            html.Div([
                html.Span(label,      className="cc-metric-label"),
                html.Span(value_str,  className="cc-metric-value"),
                html.Span(f"{pct}%",  className="cc-metric-pct"),
            ], className="cc-metric-header"),
            _bar(pct, color),
       ], className="cc-metric-row")

    def _agency_col(name, ev, mins, cmi, color):
        return html.Div([
            html.Span(name, className="cc-agency-name",
                      style={"color": color, "fontWeight": "700"}),
            html.Div([
                html.Span(f"⚡ {int(ev):,}", className="cc-agency-val"),
                html.Span(" evts", className="cc-agency-unit"),
           ]),
            html.Div([
                html.Span(f"⚠️ {float(mins):,.0f}", className="cc-agency-val"),
                html.Span(" mins", className="cc-agency-unit"),
            ]),
            html.Div([
                html.Span(f"👥 {float(cmi):,.0f}", className="cc-agency-val"),
                html.Span(" CMI", className="cc-agency-unit"),
            ]),
        ], className="cc-agency-col")

    cc_cards = []
    for _, row in cc.iterrows():
        cc_name   = ZONE_LABEL.get(row.get("control_center", ""), row.get("control_center", ""))
        cc_ev     = int(row.get("events",       0))
        cc_mins   = float(row.get("total_mins", 0))
        cc_cmi    = float(row.get("total_cmi",  0))
        ev_pct    = round(cc_ev   / tot_ev  * 100, 1)
        mins_pct  = round(cc_mins / tot_min * 100, 1)
        cmi_pct   = round(cc_cmi  / tot_cmi * 100, 1)

        b_ev   = int(row.get("bescom_events", 0))
        b_mins = float(row.get("bescom_mins", 0))
        b_cmi  = float(row.get("bescom_cmi",  0))
        k_ev   = int(row.get("kptcl_events",  0))
        k_mins = float(row.get("kptcl_mins",  0))
        k_cmi  = float(row.get("kptcl_cmi",   0))
        tot_fdr      = int(row.get("feeders",            0))
        fdr_hit      = int(row.get("feeders_interrupted", 0))
        fdr_ok       = int(row.get("feeders_healthy",     0))
        hit_pct      = round(fdr_hit / max(tot_fdr, 1) * 100, 1)
        ok_pct       = round(fdr_ok  / max(tot_fdr, 1) * 100, 1)
        # health bar: green = healthy portion, red = interrupted portion
        health_block = html.Div([
            html.Div([
                html.Span("Feeder Health", className="cc-metric-label"),
                html.Span(
                    f"{fdr_ok:,} / {tot_fdr:,}",
                    className="cc-metric-value"
                ),
                html.Span(f"{ok_pct}%", className="cc-metric-pct",
                          style={"color": "#2e8b40"}),
            ], className="cc-metric-header"),
            # two-tone bar: green healthy | red interrupted
            html.Div([
                html.Div(className="cc-metric-fill",
                         style={"width": f"{ok_pct:.1f}%",
                                "background": "#2e8b40"}),
                html.Div(className="cc-metric-fill",
                         style={"width": f"{hit_pct:.1f}%",
                                "background": "#e03c3c"}),
            ], className="cc-metric-track cc-health-track"),
            html.Div([
                html.Span("✔ ", style={"color": "#2e8b40", "fontSize": "20px"}),
                html.Span(f" {fdr_ok:,} un-interrupted ",
                          className="cc-health-note ok"),
                html.Span("  ✖", style={"color": "#e03c3c", "fontSize": "20px"}),
                html.Span(f" {fdr_hit:,} interrupted",
                          className="cc-health-note hit"),
            ], className="cc-health-legend"),
        ], className="cc-metric-row")
        card = html.Div([
            html.P(cc_name, className="cc-label"),
            html.H3(f"{int(row.get('feeders', 0)):,} feeders", className="cc-value"),
            html.P(f"{int(row.get('stations', 0))} stations · feeder universe",
                   className="cc-detail"),
            html.Hr(className="cc-divider"),
            #_metric_row("Events",      f"{cc_ev:,}",      ev_pct,   "#0098d4"),
            #_metric_row("Outage Mins", f"{cc_mins:,.0f}", mins_pct, "#e09000"),
            #_metric_row("CMI",         f"{cc_cmi:,.0f}",  cmi_pct,  "#7b3fa0"),
            health_block,
            html.Hr(className="cc-divider"),
            html.Div([
                _agency_col("🔵 BESCOM", b_ev, b_mins, b_cmi, "#0098d4"),
                html.Div(className="cc-agency-divider"),
                _agency_col("🟡 KPTCL",  k_ev, k_mins, k_cmi, "#e09000"),
            ], className="cc-agency-grid"),
        ], className="cc-base-card")

        cc_cards.append(dbc.Col(card, xs=12, md=6))

    if not top.empty:
        top = top.copy()
        top["label"] = top["division"] + " · " + top["subdivision"].fillna("")
        fig_top = build_hbar_fig(top, "minutes", "label", "events", height=320)
    else:
        fig_top = go.Figure().update_layout(**_base_layout(height=320, margin=dict(t=10, b=40, l=10, r=20)))

    colors = ["danger", "warning", "info", "secondary"]
    labels = ["🔴 Escalate", "🟡 Watch", "🔵 Monitor", "⚪ Note"]
    alerts = [
        dbc.Alert([html.Strong(f"{labels[i]}: "), f"{row['label']} — {int(row['events'])} outages · {row['minutes']:,.0f} min"], color=colors[i])
        for i, (_, row) in enumerate(top.head(4).iterrows())
    ] if not top.empty else [html.P("No data for selected period.", className="text-muted")]

    return kpi_row, reliability_row, cc_cards, build_trend_fig(daily, 280), fig_top, alerts

# ── CC Tab callbacks ──────────────────────────────────────────────────────────
def _register_cc_callbacks(sfx: str):
    @app.callback(
    Output(f"dd-div-{sfx}", "options"),
    Output(f"dd-div-{sfx}", "value"),
    Input(f"dd-cc-{sfx}", "value"),
    )
    def update_divisions(cc):
        if not cc: return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs, seen = [], set()
        for z in cc_list:
            for d in get_divisions(z):
                if d not in seen:
                    seen.add(d)
                    divs.append(d)
        return [{"label": d, "value": d} for d in divs], []

    @app.callback(
        Output(f"dd-stn-{sfx}-cat", "options"),
        Output(f"dd-stn-{sfx}-cat", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
        Input(f"dd-stn-{sfx}", "value"),
    )
    def update_feeder_categories(cc, div, stn):
        if not cc or not div: return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        cats, seen = [], set()
        for z in cc_list:
          for d in divs:
            for c in get_feeder_categories(z, d, stn if stn else None):
                if c not in seen:
                    seen.add(c)
                    cats.append(c)
        return [{"label": c, "value": c} for c in cats], []

    @app.callback(
        Output(f"dd-stn-{sfx}", "options"),
        Output(f"dd-stn-{sfx}", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
        Input(f"chart-stn-{sfx}", "clickData"),   # ← new input
    )
    def update_stations(cc, div, click_data):
        triggered = ctx.triggered_id
        # Bar click — just set the value, keep existing options
        
        if triggered == f"chart-stn-{sfx}" and click_data:
            try:    
                station_name = click_data["points"][0]["y"]
        # Rebuild options for current cc/div so the value is valid
        
                if not cc or not div:
                    return no_update, station_name
                cc_list = cc if isinstance(cc, list) else [cc]
                divs = div if isinstance(div, list) else [div]
                stns = list({s for z in cc_list for d in divs for s in get_stations(z, d)})
                opts = [{"label": s, "value": s} for s in stns]
                return opts, station_name
            except (KeyError, IndexError, TypeError):
                return no_update, no_update
                
        # Normal cascade — cc or div changed
        if not cc or not div:
            return [], None
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        stns = list({s for z in cc_list for d in divs for s in get_stations(z, d)})
        return [{"label": s, "value": s} for s in stns], None

    @app.callback(
        Output(f"dd-fdr-{sfx}", "options"),
        Output(f"dd-fdr-{sfx}", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
        Input(f"dd-stn-{sfx}", "value"),
        Input(f"dd-stn-{sfx}-cat", "value"),
    )
    def update_feeders(cc, div, stn, fdr_cat):
        if not cc or not div or not stn: return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        cat_filter = fdr_cat if fdr_cat else None
        fdrs = []
        seen = set()
        for z in cc_list:
          for d in divs:
            for f in get_feeders(z, d, stn, feeder_category=cat_filter):
                if f not in seen:
                    seen.add(f)
                    fdrs.append(f)
        return [{"label": f, "value": f} for f in fdrs], []

    # ── Main data callback ────────────────────────────────────────────────────
    @app.callback(
        Output(f"breadcrumb-{sfx}",   "children"),
        Output(f"kpi-row-{sfx}",      "children"),
        Output(f"reliability-row-{sfx}", "children"),   # ← NEW Explorer Layout Changes
        Output(f"chart-trend-{sfx}",  "figure"),
        Output(f"chart-stn-{sfx}",    "figure"),
        Output(f"table-limit-note-{sfx}", "children"),
        Output(f"store-tbl-full-{sfx}", "data"),
        Output(f"store-tbl-{sfx}",    "data"),
        Output(f"store-cols-{sfx}",   "data"),
        Input(f"btn-apply-{sfx}",     "n_clicks"),
        Input("store-date-range",     "data"),
        Input("store-data-version",   "data"),
        Input(f"trend-gran-{sfx}", "value"),
        Input(f"station-metric-{sfx}", "value"),
        Input("tabs", "active_tab"),              # ← NEW
        State(f"dd-cc-{sfx}",         "value"),
        State(f"dd-div-{sfx}",        "value"),
        State(f"dd-stn-{sfx}",        "value"),
        State(f"dd-stn-{sfx}-cat",    "value"),
        State(f"dd-fdr-{sfx}",        "value"),
        State(f"dd-otype-{sfx}",      "value"),
        State(f"dd-agency-{sfx}",     "value"),
        State("store-level-mode",     "data"),
        prevent_initial_call=True,
    )
    def update_content(n, date_store, _version, trend_gran, station_metric, active_tab, cc, div, stn, fdr_cat, fdrs, otype, agency, level_mode):
        # ── Lazy load guard ──────────────────────────────────────────────────
        # Determine which tab this instance belongs to
        my_tab = f"tab-{sfx}"   # "tab-ex"
        triggered_id = ctx.triggered_id
        
        # Skip render if:
        # 1. Active tab is NOT this tab, AND
        # 2. Trigger was NOT the Apply button (user explicitly asked for refresh)
        if active_tab != my_tab and triggered_id != f"btn-apply-{sfx}":
            return no_update, no_update, no_update, no_update, \
                   no_update, no_update, no_update, no_update, no_update
                           
        ds = (date_store or {}).get("start") or _min_dt
        de = (date_store or {}).get("end")   or _max_dt

        if isinstance(cc, list):
            cc_label = " & ".join(ZONE_LABEL.get(z, z) for z in cc) if cc else None
        else:
            cc_label = ZONE_LABEL.get(cc, cc) if cc else None
        parts = [_label_value(p) for p in [cc_label, div, stn] if p]
        if fdr_cat:
            cats_label = fdr_cat if isinstance(fdr_cat, list) else [fdr_cat]
            parts.append(f"Cat: {', '.join(cats_label)}")
        if fdrs:
            parts.append(f"{len(fdrs)} feeder(s)")
        crumb = "📍 " + (" › ".join(parts) if parts else "All networks")
        crumb += f" · {ds} → {de}"

        cat_arg = fdr_cat or None
        # Single query — df_kpi is the full result used for KPI cards and charts
        # df is sliced to MAX_TABLE_ROWS_UI for the DataTable (browser performance)
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_kpi   = pool.submit(get_interruption_table, cc, div, stn, fdrs or None,
                              otype, agency, ds, de, level_mode, feeder_category=cat_arg)
            f_sdf   = pool.submit(get_division_summary,   cc or None,
                              div, ds, de, level_mode, feeder_category=cat_arg)
            f_daily = pool.submit(get_daily_trend_agg,    cc=cc, division=div,
                              date_start=ds, date_end=de, level_mode=level_mode,
                              feeder_category=cat_arg, granularity=trend_gran or "day")
                              
        #df_kpi = get_interruption_table(cc , div, stn, fdrs or None, otype, agency, ds, de,
        #                         level_mode=level_mode, feeder_category=cat_arg)
        #sdf = get_division_summary(cc or ("BICC-1" if sfx == "cc1" else "BICC-2"), div, ds, de,
        #                    level_mode=level_mode, feeder_category=cat_arg)
        #daily = get_daily_trend_agg(cc=cc, division=div, date_start=ds, date_end=de,
        #                            level_mode=level_mode, feeder_category=cat_arg,
        #                            granularity=trend_gran or "day")
        df_kpi = f_kpi.result()
        df = df_kpi.iloc[:MAX_TABLE_ROWS_UI] if len(df_kpi) > MAX_TABLE_ROWS_UI else df_kpi
        sdf    = f_sdf.result()
        daily  = f_daily.result()
        
        tot_ev  = len(df_kpi)
        tot_min = df_kpi["Duration (min)"].sum() if "Duration (min)" in df_kpi.columns else 0
        avg_min = tot_min / max(tot_ev, 1)
        pct_u   = ((df_kpi["Type"] == "Unscheduled").sum() / max(tot_ev, 1) * 100
                   if "Type" in df_kpi.columns else 0)
        tot_cust_cc = df_kpi["Customers Affected"].sum() if "Customers Affected" in df_kpi.columns else 0
        unique_cust_cc = _unique_feeder_customers(df_kpi)
        tot_cmi_cc  = (float(df_kpi["CMI"].sum()) if "CMI" in df_kpi.columns else 0.0)
        mode_label = {"feeder": "Feeder only", "equipment": "Equipment only", "both": "Feeder + Equipment"}.get(level_mode, "Feeder + Equipment")
        # ── SAIDI — resolve denominator based on active filters ──────────────
        saidi_base, saidi_scope = resolve_saidi_base(cc, div, stn, fdrs)
        saidi_cc = round(tot_cmi_cc / saidi_base, 4) if saidi_base > 0 else 0.0
        saifi_cc = round(tot_cust_cc / saidi_base, 4) if saidi_base > 0 else 0.0   # ← NEW Explorere SAIFI Feature
        
        # Customers Affected penetration % against the filtered base
        cust_pct_cc = round(unique_cust_cc / saidi_base * 100, 2) if saidi_base > 0 else 0.0
        
        kpi_row = [
                dbc.Col(kpi_card("Total Outage Events", f"{tot_ev:,}", mode_label, "bolt",
                    raw_value=tot_ev, warn=KPI_EVENTS_WARN, danger=KPI_EVENTS_DANGER), md=True),
                dbc.Col(kpi_card("Total Minutes", f"{tot_min:,.1f}", "Outage duration", "warning",
                    raw_value=tot_min, warn=KPI_MINS_WARN, danger=KPI_MINS_DANGER), md=True),
                dbc.Col(kpi_card("Unique Customers Affected", f"{unique_cust_cc:,}", saidi_scope, "people",
                    raw_value=unique_cust_cc, warn=KPI_CUST_WARN, danger=KPI_CUST_DANGER), md=True),
                dbc.Col(kpi_card("CMI", f"{tot_cmi_cc:,.0f}", "Customer Minutes Interrupted", "cmi",
                    raw_value=tot_cmi_cc, warn=KPI_CMI_WARN, danger=KPI_CMI_DANGER), md=True),
            ]

        reliability_row = [
            dbc.Col(kpi_card("SAIDI", f"{saidi_cc:.4f}", f"min/customer • {saidi_scope}", "clock",
                tooltip_lines=[
                    "System Avg Interruption Duration Index",
                    f"Formula: CMI ÷ {saidi_scope}",
                    f"CMI: {tot_cmi_cc:,.0f} min",
                    f"Base: {saidi_base:,} customers",
                    f"Warn ≥ {KPI_SAIDI_WARN_CC}, Critical ≥ {KPI_SAIDI_DANGER_CC}",
                ], raw_value=saidi_cc, warn=KPI_SAIDI_WARN_CC, danger=KPI_SAIDI_DANGER_CC), md=True),
            dbc.Col(kpi_card("SAIFI", f"{saifi_cc:.4f}", f"interruptions/customer • {saidi_scope}", "bolt",
                tooltip_lines=[
                    "System Avg Interruption Frequency Index",
                    f"Formula: Customers Affected ÷ {saidi_scope}",
                    f"Customers Affected: {tot_cust_cc:,}",
                    f"Base: {saidi_base:,} customers",
                    f"Warn ≥ {KPI_SAIFI_WARN_CC}, Critical ≥ {KPI_SAIFI_DANGER_CC}",
                ], raw_value=saifi_cc, warn=KPI_SAIFI_WARN_CC, danger=KPI_SAIFI_DANGER_CC), md=True),
            ]
        """kpi_row = [
         dbc.Col(kpi_card("Events Selected", f"{tot_ev:,}", mode_label, "bolt",
             tooltip_lines=[f"Filter: {crumb}", f"Scope: {mode_label}", f"Warn ≥ {KPI_EVENTS_WARN:,} | Critical ≥ {KPI_EVENTS_DANGER:,}"],
             raw_value=tot_ev, warn=KPI_EVENTS_WARN, danger=KPI_EVENTS_DANGER), md=True),
         dbc.Col(kpi_card("Total Minutes", f"{tot_min:,.1f}", "Outage duration", "warning",
             tooltip_lines=[f"Avg per event: {avg_min:.1f} min", f"Scope: {mode_label}", f"Warn ≥ {KPI_MINS_WARN:,} | Critical ≥ {KPI_MINS_DANGER:,}"],
             raw_value=tot_min, warn=KPI_MINS_WARN, danger=KPI_MINS_DANGER), md=True),
         dbc.Col(kpi_card(
                    "Customers Affected",
                    f"{tot_cust_cc:,}",
                    f"of {saidi_base:,} total · {cust_pct_cc:.1f}%",
                    "people",
                    tooltip_lines=[
                        f"Affected  : {tot_cust_cc:,}",
                        f"Base      : {saidi_base:,} ({saidi_scope})",
                        f"Impact    : {cust_pct_cc:.1f}% of scoped base",
                        f"Scope     : {mode_label}",
                        f"Warn ≥ {KPI_CUST_WARN:,}, Critical ≥ {KPI_CUST_DANGER:,}",
                    ],
                    raw_value=tot_cust_cc, warn=KPI_CUST_WARN, danger=KPI_CUST_DANGER,
                    pct=cust_pct_cc,                                    # ← progress bar
                ), md=True),
         dbc.Col(kpi_card("CMI", f"{tot_cmi_cc:,.0f}", "Cust. Minutes Interrupted", "cmi",
             tooltip_lines=["CMI = Σ(customers_affected × duration_mins)", f"Scope: {mode_label}", f"Warn ≥ {KPI_CMI_WARN:,} | Critical ≥ {KPI_CMI_DANGER:,}"],
             raw_value=tot_cmi_cc, warn=KPI_CMI_WARN, danger=KPI_CMI_DANGER), md=True),
        dbc.Col(kpi_card(                           # ← NEW
                "SAIDI",
                f"{saidi_cc:.4f}",
                f"min / customer · {saidi_scope}",
                "clock",
                tooltip_lines=[
                    "System Average Interruption Duration Index",
                    "Formula  : CMI ÷ Customers in scope",
                    f"CMI      : {tot_cmi_cc:,.0f} min",
                    f"Scope    : {saidi_scope}",
                    f"Base     : {saidi_base:,} customers",
                    f"SAIDI    : {saidi_cc:.4f} min/customer",
                    f"Warn ≥ {KPI_SAIDI_WARN_CC}, Critical ≥ {KPI_SAIDI_DANGER_CC}",
                ],
                raw_value=saidi_cc,
                warn=KPI_SAIDI_WARN_CC,
                danger=KPI_SAIDI_DANGER_CC,
            ), md=True),
        ]"""

        fig_trend = build_trend_fig(daily, granularity=trend_gran or "day")
        try:
            fig_stn = build_station_fig(sdf, metric=station_metric or "total_mins", color_by_division=False)
        except Exception:
            fig_stn = go.Figure().update_layout(**_base_layout(
                height=420, margin=dict(t=10, b=40, l=10, r=20)
            ))

        cols = list(df.columns) if not df.empty else []
        table_note = html.Div(
            f"Table view limited to first {MAX_TABLE_ROWS_UI:,} rows for performance. KPI cards and charts use the full filtered dataset.",
            className="table-limit-banner"
        ) if len(df_kpi) > MAX_TABLE_ROWS_UI else ""
        
        # CHart Columns to be dislpayed vs Charts
        CHART_COLS = ["Date", "Feeder", "Division", "Sub-Division", "Station", "Type",
              "Agency", "Duration (min)", "Customers Affected", "CMI", "Cause"]
              
        chart_cols_present = [c for c in CHART_COLS if c in df_kpi.columns]
        return (crumb, kpi_row, reliability_row, fig_trend, fig_stn, table_note,
                df_kpi[chart_cols_present].to_dict("records"),   # store-tbl-full (charts only)
                df.to_dict("records"),                           # store-tbl (table view, sliced)
                cols,
                )

    # ── Render table from store (update existing DataTable props) ───────────

    @app.callback(
        Output(f"store-selected-{sfx}", "data"),
        Output(f"row-count-{sfx}", "children"),
        Input(f"btn-topn-{sfx}", "n_clicks"),
        Input(f"store-tbl-{sfx}", "data"),
        State(f"topn-input-{sfx}", "value"),
        prevent_initial_call=False,
    )
    def select_top_n(n_clicks, records, n_rows):
        """Top-N based on DB-filtered store-tbl dataset.
        - store-tbl changes  → reset selection, show total row count
        - btn-topn clicked   → select first N rows, show "N rows · M selected"
        """
        total     = len(records) if records else 0
        triggered = ctx.triggered_id
        if triggered == f"btn-topn-{sfx}" and records and n_rows and int(n_rows) >= 1:
            n        = min(int(n_rows), total)
            selected = list(range(n))
            return selected, f"{total:,} rows · {n} selected"
        return [], f"{total:,} rows"

    @app.callback(
        Output(f"modal-cols-{sfx}", "is_open"),
        Output(f"col-checklist-{sfx}", "options"),
        Output(f"col-checklist-{sfx}", "value"),
        Input(f"btn-cols-{sfx}",       "n_clicks"),
        Input(f"btn-cols-apply-{sfx}", "n_clicks"),
        State(f"modal-cols-{sfx}", "is_open"),
        State(f"store-tbl-full-{sfx}", "data"),
        State(f"store-cols-{sfx}", "data"),
        prevent_initial_call=True,
    )
    def toggle_col_modal(open_clicks, apply_clicks, is_open, records, current_cols):
        triggered = ctx.triggered_id
        if triggered == f"btn-cols-{sfx}":
            all_cols = list(records[0].keys()) if records else []
            opts = [{"label": c, "value": c} for c in all_cols]
            return True, opts, current_cols or all_cols
        return False, no_update, no_update

    @app.callback(
        Output(f"store-cols-{sfx}", "data", allow_duplicate=True),
        Input(f"btn-cols-apply-{sfx}", "n_clicks"),
        State(f"col-checklist-{sfx}", "value"),
        prevent_initial_call=True,
    )
    def apply_col_selection(n, selected):
        return selected or no_update

    # ── CSV Export (uses selected rows if any, else all filtered rows) ─────
    @app.callback(
        Output(f"download-{sfx}", "data"),
        Input(f"btn-export-{sfx}", "n_clicks"),
        State(f"store-tbl-full-{sfx}", "data"),
        State(f"store-cols-{sfx}", "data"),
        State(f"store-selected-{sfx}", "data"),
        prevent_initial_call=True,
    )
    def export_csv(n, records, cols, selected_rows):
        if not records: return no_update
        df_exp = pd.DataFrame(records)
        # If rows are selected, export only those
        if selected_rows:
            df_exp = df_exp.iloc[selected_rows]
        if cols:
            df_exp = df_exp[[c for c in cols if c in df_exp.columns]]
        suffix = f"_top{len(selected_rows)}" if selected_rows else "_all"
        return dcc.send_data_frame(df_exp.to_csv,
                                   f"bicc_{sfx}{suffix}_export.csv", index=False)

    # ── Bar Chart modal open ─────────────────────────────────────────────────
    @app.callback(
    Output(f"modal-chart-modal-{sfx}", "is_open"),
    Output(f"chart-x-{sfx}", "options"),
    Output(f"chart-y-{sfx}", "options"),
    Output(f"chart-y-{sfx}", "value"),
    Input(f"btn-chart-{sfx}", "n_clicks"),
    State(f"store-tbl-full-{sfx}", "data"),
    State(f"store-cols-{sfx}", "data"),
    State(f"store-selected-{sfx}", "data"),
    prevent_initial_call=True,
    )
    def open_chart_modal(n, records, cols, selected_rows):
        if not records:
            return False, [], [], None

        df_tmp = pd.DataFrame(records)
        if selected_rows:
            df_tmp = df_tmp.iloc[selected_rows]
        if cols:
            df_tmp = df_tmp[[c for c in cols if c in df_tmp.columns]]

        # X-axis: all columns
        all_opts = [{"label": c, "value": c} for c in df_tmp.columns]

        # Y-axis: only meaningful metric columns
        y_opts = []
        dur_candidates = [c for c in df_tmp.columns if "duration" in c.lower() or "min" in c.lower()]
        if dur_candidates:
            y_opts.append({"label": f"Duration ({dur_candidates[0]})", "value": dur_candidates[0]})
        if "CMI" in df_tmp.columns:
            y_opts.append({"label": "CMI (Sum)", "value": "CMI"})
        if "Customers Affected" in df_tmp.columns:
            y_opts.append({"label": "Customers Affected (Max/Feeder)", "value": "Customers Affected"})
        # Always offer event count
        y_opts.append({"label": "Outages (Count)", "value": "__events__"})

        default_y = y_opts[0]["value"] if y_opts else None
        return True, all_opts, y_opts, default_y

    # ── Build chart figure helper ────────────────────────────────────────
        # chart_type: "bar" | "pie"
        
    def _agg_for_chart(df_tmp, x_col, y_col, dur_col):
        """
        Build aggregated DataFrame based on y_col selection.
        - Duration col  → sum
        - CMI           → sum
        - Customers Affected → sum of max-per-feeder (per x_col group)
        - __events__    → count of rows
        Returns agg df with columns: [x_col, 'value']
        """
        if y_col == "__events__":
            agg = (df_tmp.groupby(x_col)
                   .size().reset_index(name="value"))

        elif y_col == "Customers Affected" and "Customers Affected" in df_tmp.columns:
            # Sum of max-per-feeder within each x_col group
            if "Feeder" in df_tmp.columns and x_col != "Feeder":
                agg = (df_tmp.groupby([x_col, "Feeder"])["Customers Affected"]
                       .max()
                       .reset_index()
                       .groupby(x_col)["Customers Affected"]
                       .sum()
                       .reset_index(name="value"))
            else:
                # x_col IS Feeder, or no Feeder column — just take max per x_col group
                agg = (df_tmp.groupby(x_col)["Customers Affected"]
                       .max()
                       .reset_index(name="value"))
        elif y_col == "CMI" and "CMI" in df_tmp.columns:
            agg = (df_tmp.groupby(x_col)["CMI"]
                   .sum().reset_index(name="value"))

        else:
            # Duration or any other numeric col
            col = y_col if y_col in df_tmp.columns else dur_col
            agg = (df_tmp.groupby(x_col)[col]
                   .sum().reset_index(name="value"))
                   
        # ── Always attach event count for dual-axis ──────────────────────────
        events = df_tmp.groupby(x_col).size().reset_index(name="events")
        agg = agg.merge(events, on=x_col, how="left")

        return agg.sort_values("value", ascending=False).head(30)
    
    def _build_chart_fig(agg, x_col, y_col, chart_type, df_tmp=None, dur_col=None):
        """
        agg must have: x_col and 'value' column (pre-aggregated by _agg_for_chart).
        chart_type: 'bar' | 'pie' | 'stacked_bar'
        """
        # Friendly axis label
        y_label_map = {
            "__events__":        "No. of Outages",
            "CMI":               "CMI",
            "Customers Affected":"Customers Affected",
        }
        y_title = y_label_map.get(y_col, y_col)
        fig = go.Figure()

        # ── Pie ──────────────────────────────────────────────────────────────
        if chart_type == "pie":
            top20  = agg.head(20).copy()
            rest_v = agg.iloc[20:]["value"].sum() if len(agg) > 20 else 0
            if rest_v > 0:
                top20 = pd.concat(
                    [top20, pd.DataFrame({x_col: ["Others"], "value": [rest_v]})],
                    ignore_index=True,
                )
            fig.add_pie(
                labels=top20[x_col], values=top20["value"],
                textinfo="label+percent",
                hovertemplate=f"%{{label}}<br>{y_title}: %{{value:,.1f}}<br>Share: %{{percent}}<extra></extra>",
                marker=dict(colors=px.colors.qualitative.Set3,
                            line=dict(color="#ffffff", width=1.5)),
                hole=0.35,
            )
            
            title=dict(
                text=f"{y_title} by {x_col}" + (" (Log Scale)" if y_col == "CMI" else ""),
                font=dict(size=14, color=_NAVY)
            ),
            fig.update_layout(
                plot_bgcolor=_SURFACE, paper_bgcolor=_SURFACE,
                font=dict(size=12, color=_INK),
                height=430, margin=dict(t=60, b=20, l=20, r=20),
                legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=11)),
                title=dict(text=f"{y_title} by {x_col} — Share",
                           font=dict(size=14, color=_NAVY), x=0.5),
            )
            return fig

        # ── Stacked Bar ───────────────────────────────────────────────────────
        if chart_type == "stacked_bar":
            if df_tmp is not None and dur_col is not None:
                stack_col = next(
                    (c for c in ("Agency", "Type", "agency", "outage_type") if c in df_tmp.columns),
                    None
                )
            if stack_col and x_col in df_tmp.columns:
                agg_s = (
                    df_tmp.groupby([x_col, stack_col], dropna=False)[dur_col]
                    .sum()
                    .reset_index(name="value")
                )

                order_df = (
                    agg_s.groupby(x_col, as_index=False)["value"]
                    .sum()
                    .sort_values("value", ascending=False)
                )
                category_order = order_df[x_col].tolist()
            fig_s = go.Figure()
            for grp, sub in agg_s.groupby(stack_col):
                sub = sub.set_index(x_col).reindex(category_order).dropna().reset_index()
                fig_s.add_bar(
                    x=sub[x_col],
                    y=sub["value"],
                    name=str(grp),
                    hovertemplate=f"{grp}<br>%{{x}}: %{{y:,.1f}}<extra></extra>",
                )

            fig_s.update_layout(
                barmode="stack",
                plot_bgcolor=_SURFACE,
                paper_bgcolor=_SURFACE,
                font=dict(size=12, color=_INK),
                height=390,
                margin=dict(t=45, b=90, l=65, r=25),
                xaxis=dict(
                    tickangle=-35,
                    gridcolor=_DIVIDER,
                    categoryorder="array",
                    categoryarray=category_order,
                ),
                yaxis=dict(title=dur_col, gridcolor=_DIVIDER),
                title=dict(
                    text=f"Stacked {dur_col} by {x_col} / {stack_col}",
                    font=dict(size=14, color=_NAVY),
                ),
                legend=dict(orientation="h", y=1.1),
            )
            return fig_s
            # fall through to normal bar if no stack col found

        # ── Bar ───────────────────────────────────────────────────────────────
        hover_fmt = "," if y_col == "__events__" or y_col == "Customers Affected" else ",.1f"
        fig.add_bar(
            x=agg[x_col], y=agg["value"], name=y_title,
            marker=dict(
                color=agg["value"], colorscale="Blues", showscale=False,
                colorbar=dict(thickness=12, tickfont=dict(size=10)),
                line=dict(color="rgba(0,0,0,.1)", width=0.5),
            ),
            hovertemplate=f"%{{x}}<br>{y_title}: %{{y:{hover_fmt}}}<extra></extra>",
        )
        
        # Secondary line — No. of Outages on right Y-axis (skip if y_col IS events)
        if y_col != "__events__" and "events" in agg.columns:
            fig.add_scatter(
                x=agg[x_col], y=agg["events"],
                name="No. of Outages",
                yaxis="y2",
                mode="lines+markers",
                line=dict(color=_DANGER, width=2.5),
                marker=dict(size=7, color=_DANGER,
                            line=dict(color="white", width=1.5)),
                hovertemplate="%{x}<br>Outages: %{y:,}<extra></extra>",
            )
        
        # ── Y-axis config — Log scale ONLY when CMI is selected ──────────────
        if y_col == "CMI":
            y_axis_cfg = dict(
                title="CMI (Log Scale)",
                type="log",           # ← log scale ON
                tickformat=",.0f",    # readable: 1,000 not 1e+3
                dtick=1,              # gridline per power of 10
                gridcolor=_DIVIDER,
            )
            chart_title = f"CMI by {x_col} (Log Scale)"
        else:
            y_axis_cfg = dict(
                title=y_title,
                gridcolor=_DIVIDER,   # ← linear scale for ALL other metrics
            )
            chart_title = f"{y_title} by {x_col}"
            
        # Y2 only shown when outage line is present
        y2_cfg = dict(
            title="No. of Outages",
            overlaying="y", side="right",
            color=_DANGER, showgrid=False,
        ) if y_col != "__events__" else dict(visible=False)
    
        fig.update_layout(
            plot_bgcolor=_SURFACE, paper_bgcolor=_SURFACE,
            font=dict(size=12, color=_INK),
            height=390, margin=dict(t=45, b=90, l=65, r=25),
            xaxis=dict(tickangle=-35, gridcolor=_DIVIDER),
            yaxis=y_axis_cfg,
            yaxis2=y2_cfg,
            legend=dict(orientation="h", y=1.08, x=0),
            bargap=0.35,
            title=dict(text=chart_title, font=dict(size=14, color=_NAVY)),
        )
        return fig

    # ── Draw callback ─────────────────────────────────────────────────────
    @app.callback(
        Output(f"modal-chart-{sfx}", "children"),
        Output(f"store-chart-agg-{sfx}", "data"),
        Output(f"chart-toggle-strip-{sfx}", "style"),
        Output(f"modal-chart-title-{sfx}", "children"),
        Input(f"btn-draw-{sfx}", "n_clicks"),
        State(f"chart-x-{sfx}", "value"),
        State(f"chart-y-{sfx}", "value"),
        State(f"chart-type-{sfx}", "value"),
        State(f"store-tbl-full-{sfx}", "data"),
        State(f"store-cols-{sfx}", "data"),
        State(f"store-selected-{sfx}", "data"),
        prevent_initial_call=True,
    )
    def draw_chart(n, x_col, y_col, chart_type, records, cols, selected_rows):
        empty = (html.P("Select X-axis and Y-axis, then click Draw.",
                        className="text-muted text-center py-4"),
                 no_update, {"display": "none"}, "Chart Selected Data")

        if not records or not x_col or not y_col:
            return empty

        df_tmp = pd.DataFrame(records)
        if selected_rows:
            df_tmp = df_tmp.iloc[selected_rows]
        if cols:
            df_tmp = df_tmp[[c for c in cols if c in df_tmp.columns]]

        # Auto-detect duration column
        dur_candidates = [c for c in df_tmp.columns if "duration" in c.lower() or "min" in c.lower()]
        dur_col = dur_candidates[0] if dur_candidates else None

        if not dur_col and y_col not in ("CMI", "Customers Affected", "__events__"):
            return empty

        ct  = chart_type or "bar"

        # ── Aggregate ────────────────────────────────────────────────────────
        agg = _agg_for_chart(df_tmp, x_col, y_col, dur_col)
        if agg.empty:
            return empty

        # ── Build figure ─────────────────────────────────────────────────────
        fig = _build_chart_fig(agg, x_col, y_col, ct, df_tmp=df_tmp, dur_col=dur_col)

        y_label_map = {
            "__events__":        "Outages",
            "CMI":               "CMI",
            "Customers Affected":"Customers Affected",
        }
        met_label = y_label_map.get(y_col, y_col)
        icon      = "🥧" if ct == "pie" else "📊"
        title     = f"{icon} {met_label} by {x_col}"

        graph = dcc.Graph(
            figure=fig,
            config={"displayModeBar": "hover",
                    "toImageButtonOptions": {"format": "png",
                                             "filename": f"chart_{sfx}", "scale": 2}},
        )
        agg_store = {"x_col": x_col, "y_col": y_col, "data": agg.to_dict("records")}
        return graph, agg_store, {"display": "block"}, title

    # ── One-click Bar ↔ Pie toggle ────────────────────────────────────────
    @app.callback(
        Output(f"modal-chart-{sfx}", "children", allow_duplicate=True),
        Output(f"modal-chart-title-{sfx}", "children", allow_duplicate=True),
        Output(f"chart-type-{sfx}", "value"),
        Input(f"btn-to-bar-{sfx}", "n_clicks"),
        Input(f"btn-to-pie-{sfx}", "n_clicks"),
        State(f"store-chart-agg-{sfx}", "data"),
        prevent_initial_call=True,
    )
    def toggle_chart_type(to_bar, to_pie, agg_store):
        if not agg_store:
            return no_update, no_update, no_update
        triggered = ctx.triggered_id
        ct     = "bar" if triggered == f"btn-to-bar-{sfx}" else "pie"
        x_col  = agg_store["x_col"]
        y_col  = agg_store["y_col"]
        met    = agg_store.get("metric", "both")
        agg    = pd.DataFrame(agg_store["data"])
        fig    = _build_chart_fig(agg, x_col, y_col, ct, met)
        icon   = "🥧" if ct == "pie" else "📊"
        m_lbl  = {"duration": "Duration", "outages": "Outages",
                  "both": "Duration & Outages"}.get(met, met)
        title  = f"{icon} {m_lbl} by {x_col} — {'Pie' if ct == 'pie' else 'Bar'}"
        graph  = dcc.Graph(figure=fig, config={
            "displayModeBar": "hover",
            "toImageButtonOptions": {"format": "png",
                                     "filename": f"chart_{sfx}", "scale": 2},
        })
        return graph, title, ct
    # ── Build per-column filter row ───────────────────────────────────────
    @app.callback(
        Output(f"filter-row-{sfx}", "children"),
        Input(f"store-tbl-{sfx}", "data"),
        Input(f"store-cols-{sfx}", "data"),
        prevent_initial_call=False,
    )
    def build_filter_row(records, cols):
        if not records or not cols:
            return []
        df_tmp  = pd.DataFrame(records)
        visible = [c for c in cols if c in df_tmp.columns]

        FORCE_DROPDOWN = {"Division", "Sub-Division", "Station", "Feeder",
                          "Cause", "Type", "Status", "Agency"}
        NUMERIC_COLS   = {"Duration min", "Customers Affected", "CMI"}

        filters = []
        for col in visible:
            uniq = sorted(df_tmp[col].dropna().astype(str).unique().tolist())

            # Numeric range — ≥ / ≤ inputs
            if col in NUMERIC_COLS or (
                pd.api.types.is_numeric_dtype(df_tmp[col]) and col not in FORCE_DROPDOWN
            ):
                col_min = float(df_tmp[col].min()) if pd.api.types.is_numeric_dtype(df_tmp[col]) else 0
                col_max = float(df_tmp[col].max()) if pd.api.types.is_numeric_dtype(df_tmp[col]) else 0
                ctrl = html.Div([
                    dcc.Input(
                        id={"type": f"col-filter-{sfx}", "col": f"{col}__gte"},
                        type="number", placeholder=f"≥ {col_min:.1f}",
                        debounce=True, step=0.01,
                        className="dt-filter-input dt-filter-input-half",
                    ),
                    dcc.Input(
                        id={"type": f"col-filter-{sfx}", "col": f"{col}__lte"},
                        type="number", placeholder=f"≤ {col_max:.1f}",
                        debounce=True, step=0.01,
                        className="dt-filter-input dt-filter-input-half",
                    ),
                ], className="dt-filter-num-wrap")

            # Searchable multi-select dropdown
            elif col in FORCE_DROPDOWN or len(uniq) <= 200:
                ctrl = dcc.Dropdown(
                    id={"type": f"col-filter-{sfx}", "col": col},
                    options=[{"label": v, "value": v} for v in uniq],
                    placeholder=f"🔍 {col}",
                    searchable=True,
                    clearable=True,
                    multi=True,
                    className="dt-filter-dropdown",
                )

            # Text search fallback
            else:
                ctrl = dcc.Input(
                    id={"type": f"col-filter-{sfx}", "col": col},
                    type="text",
                    placeholder=f"🔍 {col}",
                    debounce=True,
                    className="dt-filter-input",
                )

            # ← append is now OUTSIDE all if/elif/else, applies to every col
            filters.append(
                html.Div([
                    html.Div(col, className="dt-filter-col-label"),
                    ctrl,
                ], className="dt-filter-cell")
            )

        return html.Div(filters, className="dt-filter-inner")

    # ── Apply column filters → store ──────────────────────────────────────
    @app.callback(
        Output(f"store-col-filters-{sfx}", "data"),
        Input({"type": f"col-filter-{sfx}", "col": ALL}, "value"),
        State({"type": f"col-filter-{sfx}", "col": ALL}, "id"),
        prevent_initial_call=True,
    )
    def collect_col_filters(values, ids):
        filters = {}
        for v, id_ in zip(values, ids):
            col = id_["col"]
            if v is not None and v != [] and v != "":
                filters[col] = v if isinstance(v, list) else [v]
        return filters

    # ── Render DataTable ──────────────────────────────────────────────────
    @app.callback(
        Output(f"datatable-{sfx}", "data"),
        Output(f"datatable-{sfx}", "columns"),
        Output(f"datatable-{sfx}", "selected_rows"),
        Output(f"datatable-{sfx}", "tooltip_data"),
        Input(f"store-tbl-{sfx}", "data"),
        Input(f"store-cols-{sfx}", "data"),
        Input(f"store-selected-{sfx}", "data"),
        Input(f"store-col-filters-{sfx}", "data"),
        Input(f"datatable-{sfx}", "sort_by"),
        prevent_initial_call=False,
    )
    def render_table(records, cols, selected_rows, col_filters, sort_by):
        if not records:
            return [], [], [], []
        df_tmp       = pd.DataFrame(records)
        visible_cols = [c for c in (cols or list(df_tmp.columns)) if c in df_tmp.columns]
        df_tmp       = df_tmp[visible_cols]

        for col, vals in (col_filters or {}).items():
            if not vals and vals != 0:
                continue

            if col.endswith("__gte"):
                base = col[:-5]
                if base in df_tmp.columns:
                    v = float(vals[0] if isinstance(vals, list) else vals)
                    df_tmp = df_tmp[pd.to_numeric(df_tmp[base], errors="coerce") >= v]

            elif col.endswith("__lte"):
                base = col[:-5]
                if base in df_tmp.columns:
                    v = float(vals[0] if isinstance(vals, list) else vals)
                    df_tmp = df_tmp[pd.to_numeric(df_tmp[base], errors="coerce") <= v]

            elif col in df_tmp.columns:
                # dropdown multi-select or text search
                if isinstance(vals, list):
                    df_tmp = df_tmp[df_tmp[col].astype(str).isin([str(v) for v in vals])]
                else:
                    df_tmp = df_tmp[
                        df_tmp[col].astype(str).str.contains(str(vals), case=False, na=False)
                    ]

        if sort_by:
            df_tmp = df_tmp.sort_values(
                [s["column_id"] for s in sort_by if s["column_id"] in df_tmp.columns],
                ascending=[s["direction"] == "asc" for s in sort_by],
            )

        columns  = [{"name": c, "id": c, "deletable": False, "selectable": True}
                    for c in visible_cols]
        data     = df_tmp.to_dict("records")
        tooltips = [{c: {"value": str(row.get(c, "")), "type": "markdown"}
                     for c in visible_cols}
                    for row in data]
        return data, columns, (selected_rows or []), tooltips
        
    @app.callback(
        Output(f"chart-pane-trend-ex", "style"),
        Output(f"chart-pane-stn-ex", "style"),
        Output(f"subtab-trend-btn-ex", "className"),
        Output(f"subtab-stn-btn-ex", "className"),
        Output(f"subtab-charts-ex", "data"),
        Input(f"subtab-trend-btn-ex", "n_clicks"),
        Input(f"subtab-stn-btn-ex", "n_clicks"),
        prevent_initial_call=True,
        )
    def toggle_explorer_chart_subtab(n_trend, n_stn):
        trig = ctx.triggered_id
        if trig == "subtab-stn-btn-ex":
            return ({"display": "none"}, {"display": "block"},
                    "btn-subtab", "btn-subtab active", "station")
        return ({"display": "block"}, {"display": "none"},
                "btn-subtab active", "btn-subtab", "trend")


_register_cc_callbacks("ex")

# ============================================================
# NEW: Lightweight cascade-only callback registrar
# Registers ONLY the 4 dropdown-cascade callbacks (Division / Feeder-Category /
# Station / Feeder) for a given suffix — no table, no chart-modal, no export,
# no station-metric radio, no subtab toggle. Safe to call for tabs like Trends
# that reuse hierarchy_dropdowns() but do NOT reuse Explorer's full data pane.
#
# Mirrors the exact cascade logic already inside _register_cc_callbacks(),
# just extracted into its own standalone function — zero new query logic,
# zero drift risk from the proven Explorer cascade.
# ============================================================

def _register_cascade_callbacks(sfx: str):

    @app.callback(
        Output(f"dd-div-{sfx}", "options"),
        Output(f"dd-div-{sfx}", "value"),
        Input(f"dd-cc-{sfx}", "value"),
    )
    def _update_divisions(cc):
        if not cc:
            return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs, seen = [], set()
        for z in cc_list:
            for d in get_divisions(z):
                if d not in seen:
                    seen.add(d)
                    divs.append(d)
        return [{"label": d, "value": d} for d in divs], []

    @app.callback(
        Output(f"dd-stn-{sfx}-cat", "options"),
        Output(f"dd-stn-{sfx}-cat", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
        Input(f"dd-stn-{sfx}", "value"),
    )
    def _update_feeder_categories(cc, div, stn):
        if not cc or not div:
            return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        cats, seen = [], set()
        for z in cc_list:
            for d in divs:
                for c in get_feeder_categories(z, d, stn if stn else None):
                    if c not in seen:
                        seen.add(c)
                        cats.append(c)
        return [{"label": c, "value": c} for c in cats], []

    @app.callback(
        Output(f"dd-stn-{sfx}", "options"),
        Output(f"dd-stn-{sfx}", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
    )
    def _update_stations(cc, div):
        if not cc or not div:
            return [], None
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        stns = []
        for z in cc_list:
            for d in divs:
                stns += get_stations(z, d)
        return [{"label": s, "value": s} for s in stns], None

    @app.callback(
        Output(f"dd-fdr-{sfx}", "options"),
        Output(f"dd-fdr-{sfx}", "value"),
        Input(f"dd-cc-{sfx}", "value"),
        Input(f"dd-div-{sfx}", "value"),
        Input(f"dd-stn-{sfx}", "value"),
        Input(f"dd-stn-{sfx}-cat", "value"),
    )
    def _update_feeders(cc, div, stn, fdrcat):
        if not cc or not div or not stn:
            return [], []
        cc_list = cc if isinstance(cc, list) else [cc]
        divs = div if isinstance(div, list) else [div]
        cat_filter = fdrcat if fdrcat else None
        fdrs, seen = [], set()
        for z in cc_list:
            for d in divs:
                for f in get_feeders(z, d, stn, feeder_category=cat_filter):
                    if f not in seen:
                        seen.add(f)
                        fdrs.append(f)
        return [{"label": f, "value": f} for f in fdrs], []
        
_register_cascade_callbacks("tr")     # NEW: cascade dropdowns only, for Trends

# ---- 4. New callbacks (all new IDs, no collision with Explorer/Overview) ----

@app.callback(
    Output(f"compare-kpi-row-tr", "children"),
    Output(f"chart-overlay-tr", "figure"),
    Output(f"store-compare-tr", "data"),
    Input("btn-compare-tr", "n_clicks"),
    State("dd-cc-tr", "value"), State("dd-div-tr", "value"), State("dd-stn-tr", "value"),
    State("dd-fdr-tr", "value"), State("dd-stn-tr-cat", "value"),
    State("dd-otype-tr", "value"), State("dd-agency-tr", "value"),
    State("period-a-tr", "start_date"), State("period-a-tr", "end_date"),
    State("period-b-tr", "start_date"), State("period-b-tr", "end_date"),
    State("store-level-mode", "data"),
    prevent_initial_call=True,
)
def update_period_comparison(n, cc, div, stn, fdrs, fdrcat, otype, agency,
                              a_start, a_end, b_start, b_end, level_mode):
    result = get_period_comparison(
        cc=cc, division=div, station=stn, feeders=fdrs or None, feeder_category=fdrcat,
        outage_type=otype, agency=agency,
        period_a_start=a_start, period_a_end=a_end,
        period_b_start=b_start, period_b_end=b_end,
        level_mode=level_mode or "both",
    )
    a, b = result["period_a"], result["period_b"]

    def _delta(v_a, v_b):
        if v_a == 0:
            return 0.0
        return round((v_b - v_a) / v_a * 100, 1)

    def _kpi(title, val_a, val_b, unit="", decimals=1, worse_if_up=True):
        d = _delta(val_a, val_b)
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        is_worse = (d > 0) if worse_if_up else (d < 0)
        cls = "delta-up" if d > 0 else ("delta-down" if d < 0 else "")
        border_color = (
            "var(--bescom-danger)" if (d != 0 and is_worse)
            else "var(--bescom-success)" if d != 0
            else "var(--bescom-border)"
        )
        val_fmt = f"{val_b:,.{decimals}f}{unit}"
        card_inner = html.Div([
            html.P(title, className="kpi-label"),
            html.H3([
                val_fmt,
                html.Span(f" {arrow} {abs(d)}%", className=f"compare-kpi-delta {cls}"),
            ], className="kpi-value"),
            html.P(f"{val_a:,.{decimals}f}{unit} in Period A", className="kpi-sub"),
        ], className="kpi-card-body", style={"padding": ".9rem 1rem"})
        return dbc.Col(
            html.Div(card_inner, className="kpi-card",
                      style={"borderLeft": f"4px solid {border_color}"}),
            md=3,
        )

    kpi_row = [
        _kpi("Total Events", a["events"], b["events"], decimals=0),
        _kpi("Total Minutes", a["total_mins"], b["total_mins"], decimals=1),
        _kpi("SAIDI", a["saidi"], b["saidi"], unit=" min/cust", decimals=4),
        _kpi("SAIFI", a["saifi"], b["saifi"], unit=" int/cust", decimals=4),
    ]

    fig = go.Figure()
    da, db_ = result["daily_a"], result["daily_b"]
    if not da.empty:
        fig.add_scatter(x=list(range(len(da))), y=da["total_events"], name="Period A",
                         line=dict(color=_PRIMARY, width=2.5))
    if not db_.empty:
        fig.add_scatter(x=list(range(len(db_))), y=db_["total_events"], name="Period B",
                         line=dict(color=_DANGER, width=2.5, dash="dash"))
    fig.update_layout(**_base_layout(height=280, margin=dict(t=30, b=40, l=52, r=30)))
    
      # ── NEW: build a JSON-serializable copy for the dcc.Store output ──────
    da_store = da.copy()
    db_store = db_.copy()
    if "event_date" in da_store.columns:
        da_store["event_date"] = da_store["event_date"].astype(str)
    if "event_date" in db_store.columns:
        db_store["event_date"] = db_store["event_date"].astype(str)
    compare_data = {
        "period_a": a,                              # plain dict of scalars — already fine
        "period_b": b,                               # plain dict of scalars — already fine
        "daily_a": da_store.to_dict("records"),      # DataFrame -> list of dicts
        "daily_b": db_store.to_dict("records"),       # DataFrame -> list of dicts
        }

    return kpi_row, fig, compare_data
    
@app.callback(
    Output(f"chart-saidi-tr", "figure"),
    Output(f"chart-saifi-tr", "figure"),
    Output(f"zone-rank-tr", "children"),
    Output(f"store-trend-tr", "data"),
    Input("btn-apply-tr", "n_clicks"),
    Input("trend-gran-tr", "value"),
    Input("reliability-view-tr", "value"),
    State("dd-cc-tr", "value"), State("dd-div-tr", "value"), State("dd-stn-tr", "value"),
    State("dd-fdr-tr", "value"), State("dd-stn-tr-cat", "value"),
    State("dd-otype-tr", "value"), State("dd-agency-tr", "value"),
    State("store-date-range", "data"),
    State("store-level-mode", "data"),
    prevent_initial_call=True,
)
def update_reliability_trends(n, granularity, view_mode, cc, div, stn, fdrs, fdrcat, otype, agency, date_store, level_mode):
    ds = (date_store or {}).get("start")
    de = (date_store or {}).get("end")
    df = get_saidi_saifi_trend(
        cc=cc, division=div, station=stn, feeders=fdrs or None, feeder_category=fdrcat,
        granularity=granularity or "month", level_mode=level_mode or "both",
        date_start=ds, date_end=de, outage_type=otype, agency=agency,
    )
    if (view_mode or "topn") == "heatmap":
        fig_saidi = _build_heatmap_fig(df, metric="saidi")
        fig_saifi = _build_heatmap_fig(df, metric="saifi")
    else:
        fig_saidi = _build_topn_trend_fig(
            df, metric="saidi", warn=KPI_SAIDI_WARN_CC, danger=KPI_SAIDI_DANGER_CC)
        fig_saifi = _build_topn_trend_fig(
            df, metric="saifi", warn=KPI_SAIFI_WARN_CC, danger=KPI_SAIFI_DANGER_CC)
    # Zone ranking — latest period, all divisions in scope, sorted by SAIDI desc
    zone_rank_children = [html.Div("No data for selected filters.", className="text-muted small py-3")]
    if not df.empty:
        periods_sorted = sorted(df["period"].unique())
        latest_period = periods_sorted[-1]
        prev_period = periods_sorted[-2] if len(periods_sorted) >= 2 else None
        latest = df[df["period"] == latest_period].sort_values("saidi", ascending=False)
        prev_lookup = (df[df["period"] == prev_period].set_index("division")
                       if prev_period is not None else None)
        max_saidi = latest["saidi"].max() or 1

        # ── Missing heading, now added to match mockup ──────────────────
        header_block = html.Div([
            html.Span("Division Reliability Ranking (SAIDI) — This Period",
                      className="chart-card-title"),
            html.Span(" reuses existing get_saidi_saifi_trend",
                      style={"fontWeight": 400, "color": _MUTED, "fontSize": "11px", "marginLeft": "6px"}),
        ], style={"marginBottom": ".5rem"})

        rows = [header_block]
        colors = [_DANGER, "#e09000", "#e09000", _SUCCESS, _SUCCESS]
        for i, (_, r) in enumerate(latest.iterrows()):
            pct = round(r["saidi"] / max_saidi * 100, 1) if max_saidi else 0
            color = colors[min(i, len(colors) - 1)]

            # ── Missing MoM % delta, now computed vs prior period ───────
            mom = 0.0
            if prev_lookup is not None and r["division"] in prev_lookup.index:
                prev_saidi = prev_lookup.loc[r["division"], "saidi"]
                mom = round((r["saidi"] - prev_saidi) / prev_saidi * 100, 1) if prev_saidi else 0.0
            mom_cls = "mom-up" if mom > 0 else ("mom-down" if mom < 0 else "mom-flat")
            arrow = "▲" if mom > 0 else ("▼" if mom < 0 else "—")

            rows.append(html.Div([
                html.Div(str(i + 1), className="zone-rank-badge", style={"background": color}),
                html.Div(r["division"], style={"width": "150px", "fontSize": "12.5px", "fontWeight": 600}),
                html.Div(html.Div(style={"width": f"{pct}%", "background": color, "height": "100%"}),
                         className="zone-rank-bar-track"),
                html.Div(f"{r['saidi']:.3f}", style={"width": "80px", "textAlign": "right", "fontWeight": 700}),
                html.Div(html.Span(f"{arrow} {abs(mom)}", className=mom_cls),
                         style={"width": "60px", "textAlign": "right"}),
            ], className="zone-rank-row"))
        zone_rank_children = rows
    #Insight Children
    insight_children = []
    if not df.empty:
        latest_period = df["period"].max()
        latest = df[df["period"] == latest_period].sort_values("saidi", ascending=False)
        if len(latest) >= 2:
            worst = latest.iloc[0]
            best = latest.iloc[-1]
            ratio = round(worst["saidi"] / best["saidi"], 1) if best["saidi"] > 0 else 0
            insight_children = [
                    html.Div([
                        html.Strong(str(worst["division"])),
                        f" is {ratio}× worse than ",
                        html.Strong(str(best["division"])),
                        " this period. Ranking recalculates live as filters change.",
                    ], className="rank-insight-text")
                ]        
    zone_rank_children = zone_rank_children + insight_children

    return fig_saidi, fig_saifi, zone_rank_children, df.to_dict("records")

@app.callback(
    Output(f"offenders-table-tr", "children"),
    Output(f"store-offenders-tr", "data"),
    Input("btn-apply-tr", "n_clicks"),
    Input("min-events-tr", "value"),
    Input(f"split-month-tr", "value"),          # NEW
    State("dd-cc-tr", "value"), State("dd-div-tr", "value"), State("dd-stn-tr", "value"),
    State("dd-fdr-tr", "value"), State("dd-stn-tr-cat", "value"),
    State("store-date-range", "data"),
    State("store-level-mode", "data"),
    prevent_initial_call=True,
)
def update_repeat_offenders(n, min_events, split_month, cc, div, stn, fdrs, fdrcat, date_store, level_mode):
    ds = (date_store or {}).get("start")
    de = (date_store or {}).get("end")
    df = get_repeat_offenders(
        cc=cc, division=div, station=stn, feeders=fdrs or None, feeder_category=fdrcat,
        min_events=min_events or 3, level_mode=level_mode or "both",
        date_start=ds, date_end=de,
    )
    if df.empty:
        return _empty_state("No repeat offenders", "No feeders meet the minimum event threshold for this period."), []

    month_cols = [c for c in df.columns if c not in
                  ("feeder", "division", "station", "total_events", "total_cmi", "mom_pct", "pct_zone_cmi")]
                  
    def _month_label(col):
        try:
            return pd.to_datetime(col, format="%Y-%m").strftime("%b-%y").upper()
        except Exception:
            return str(col)
            
    month_labels = {c: _month_label(c) for c in month_cols} 

    split_on = bool(split_month and "split" in split_month)
        
    def _derive_flag(row, month_cols):
        vals = [int(row[m]) for m in month_cols]
        if len(vals) < 2:
            return None
        last, prev = vals[-1], vals[-2]
        peak = max(vals)
        if last >= prev and last >= peak * 0.8 and peak > 0:
            return ("REPEAT", "flag-repeat")
        if last < peak * 0.6 and len(vals) >= 3 and vals[-1] <= vals[-2] <= vals[-3]:
            return ("IMPROVING", "flag-improving")
        return ("WATCH", "flag-watch")
    
    def _heat_class(v):
        if v == 0:
            return "heat-0"
        elif v == 1:
            return "heat-low"
        elif v <= 3:
            return "heat-mid"
        elif v <= 5:
            return "heat-high"
        else:
            return "heat-crit"
            
    def _build_collapsed_offenders_table(df, month_cols):
        header = html.Tr([html.Th(c) for c in
            ["Feeder", "Division", "Station", "Total Events (period)",
             "MoM %", "Total Events", "Total CMI", "% of Zone CMI"]])
        body_rows = []
        for _, r in df.iterrows():
            flag_label, flag_cls = _derive_flag(r, month_cols) or (None, None)
            feeder_cell = html.Td([
                r["feeder"],
                html.Span(flag_label, className=f"offender-flag {flag_cls}") if flag_label else None,
            ])
            mom = r.get("mom_pct", 0)
            mom_cls = "mom-up" if mom > 0 else ("mom-down" if mom < 0 else "mom-flat")
            arrow = "▲" if mom > 0 else ("▼" if mom < 0 else "—")
            cells = [
                feeder_cell, html.Td(r["division"]), html.Td(r["station"]),
                html.Td(str(sum(int(r[m]) for m in month_cols))),
                html.Td(html.Span(f"{arrow} {abs(mom)}%", className=mom_cls)),
                html.Td(html.Strong(str(r["total_events"]))),
                html.Td(f"{r['total_cmi']:,}"),
                html.Td(f"{r['pct_zone_cmi']}%"),
            ]
            body_rows.append(html.Tr(cells))
        return html.Table([html.Thead(header), html.Tbody(body_rows)],
                           className="month-pivot-table")

    header = html.Tr([html.Th(c) for c in ["Feeder", "Division", "Station"] + [html.Th(month_labels[c]) for c in month_cols] + ["M-o-M %", "Total Events", "Total CMI", "% of Zone CMI"]])
    body_rows = []
    for _, r in df.iterrows():
        flag_label, flag_cls = _derive_flag(r, month_cols) or (None, None)
        feeder_cell = html.Td([
            r["feeder"],
            html.Span(flag_label, className=f"offender-flag {flag_cls}") if flag_label else None,
        ])
        cells = [feeder_cell, html.Td(r["division"]), html.Td(r["station"])]
        cells += [html.Td(html.Span(int(r[m]), className=f"heat-cell {_heat_class(int(r[m]))}")) for m in month_cols]
        #cells += [html.Td(html.Span(str(int(val)), className=f"heat-cell {heat_class(val)}"),key=month_col)]
        mom_cls = "mom-up" if r["mom_pct"] > 0 else ("mom-down" if r["mom_pct"] < 0 else "mom-flat")
        arrow = "▲" if r["mom_pct"] > 0 else ("▼" if r["mom_pct"] < 0 else "—")
        cells.append(html.Td(html.Span(f"{arrow} {abs(r['mom_pct'])}%", className=mom_cls)))
        cells.append(html.Td(html.Strong(int(r["total_events"]))))
        cells.append(html.Td(f"{r['total_cmi']:,.0f}"))
        cells.append(html.Td([
            html.Div(className="cmi-contrib-bar-track", children=[
                html.Div(className="cmi-contrib-bar-fill",
                          style={"width": f"{min(r['pct_zone_cmi'], 100)}%"}),
            ]),
            html.Span(f"{r['pct_zone_cmi']}%"),
        ]))
        body_rows.append(html.Tr(cells))

    table = html.Table([html.Thead(header), html.Tbody(body_rows)], className="month-pivot-table")
    if split_on:
        table = table   # existing per-month table
    else:
        table = _build_collapsed_offenders_table(df, month_cols)
        
    return html.Div(table, style={"overflowX": "auto"}), df.to_dict("records")
    
@app.callback(
    Output(f"download-offenders-tr", "data"),
    Input(f"btn-export-offenders-tr", "n_clicks"),
    State(f"store-offenders-tr", "data"),
    prevent_initial_call=True,
)
def export_offenders(n, stored_data):
    if not stored_data:
        return no_update
    df = pd.DataFrame(stored_data)
    return dcc.send_data_frame(df.to_csv, "repeat_offenders.csv", index=False)

#---------TOGGLE TRENDS SUBTAB CALLBACKS (Compare / Reliability) ---------
@app.callback(
    Output(f"pane-compare-tr", "style"),
    Output(f"pane-reliability-tr", "style"),
    Output(f"subtab-compare-btn-tr", "className"),
    Output(f"subtab-reliability-btn-tr", "className"),
    Output(f"store-subtab-tr", "data"),
    Input(f"subtab-compare-btn-tr", "n_clicks"),
    Input(f"subtab-reliability-btn-tr", "n_clicks"),
    Input(f"btn-apply-tr", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_trends_subtab(n_compare, n_reliability, n_apply):
    trig = ctx.triggered_id
    if trig in ("subtab-reliability-btn-tr", "btn-apply-tr"):
        return (
            {"display": "none"}, {"display": "block"},
            "btn-subtab", "btn-subtab active", "reliability",
        )
    return (
        {"display": "block"}, {"display": "none"},
        "btn-subtab active", "btn-subtab", "compare",
    )


# ── Admin callbacks ───────────────────────────────────────────────────────────

@app.callback(
    Output("admin-login-panel", "style"),
    Output("admin-console",     "style"),
    Output("admin-login-msg",   "children"),
    Output("store-admin-auth",  "data"),
    Input("init-date-interval", "n_intervals"),
)
def admin_access_check(n):
    if is_bicc_admin():
        return {"display": "none"}, {"display": "block"}, "", True
    return (
        {"display": "block"}, {"display": "none"},
        "You're signed in but not a member of the admin group. "
        "Contact IT if you believe this is incorrect.",
        False,
    )


@app.callback(
    Output("download-sample-xlsx", "data"),
    Input("btn-download-sample",   "n_clicks"),
    prevent_initial_call=True,
)
def download_sample(n):
    import os
    sample_path = "interruption_data.xlsx"
    if os.path.exists(sample_path):
        return dcc.send_file(sample_path, filename="sample_interruption_format.xlsx")
    return no_update


@app.callback(
    Output("admin-upload-status",       "children"),
    Output("store-db-stats",            "data"),
    Output("store-data-version",        "data",            allow_duplicate=True),
    Output("global-date-range",         "min_date_allowed", allow_duplicate=True),
    Output("global-date-range",         "max_date_allowed", allow_duplicate=True),
    Output("global-date-range",         "start_date",       allow_duplicate=True),
    Output("global-date-range",         "end_date",         allow_duplicate=True),
    Input("upload-data",                "contents"),
    State("upload-data",                "filename"),
    State("store-admin-auth",           "data"),
    State("store-data-version",         "data"),
    prevent_initial_call=True,
)

def handle_upload(contents, filename, is_auth, current_version):

    if not is_auth:
        return (dbc.Alert("Please login first.", color="warning"),
                no_update, no_update, no_update, no_update, no_update, no_update)
    if contents is None:
        return (no_update, no_update, no_update,
                no_update, no_update, no_update, no_update)
                
    import base64, io as _io
    from delta_load import delta_insert_tracked

    # ── Stage 1: Decode ─────────────────────────────────────────────────
    try:
        _content_type, content_string = contents.split(",")
        file_bytes = base64.b64decode(content_string)
        # Quick pre-validation: read into DataFrame just to run _validate_upload_df
        raw_df = pd.read_excel(_io.BytesIO(file_bytes))
    except Exception as e:
        return (dbc.Alert(f"❌ Could not decode upload: {e}", color="danger"),
                no_update, no_update, no_update, no_update, no_update, no_update)

    # ── Stage 2: Read into DataFrame for validation ──────────────────────
    try:
        import io as _io
        raw_df = pd.read_excel(_io.BytesIO(file_bytes))
    except Exception as e:
        return (dbc.Alert(f"❌ Could not read file: {e}", color="danger"),
                no_update, no_update, no_update, no_update, no_update, no_update)

    # ── Stage 3: Validate ────────────────────────────────────────────────
    ok, errors, warnings = _validate_upload_df(raw_df)
    if not ok:
        err_list = html.Ul([html.Li(e) for e in errors])
        return (dbc.Alert(["❌ Validation failed", err_list], color="danger"), no_update, no_update, no_update, no_update, no_update, no_update)

    # ── Stage 4: Delta insert ────────────────────────────────────────────
    try:
        result = delta_insert_tracked(file_bytes, filename or "upload.xlsx", db_path=DB_PATH)
        if "error" in result:
            raise RuntimeError(result["error"])
            
        n_inserted  = result["inserted"]
        n_dupes     = result["skipped"]
        rows_before = result["rows_before"]
        rows_after  = result["rows_after"]
        date_min    = result["date_min"]
        date_max    = result["date_max"]
        new_max     = str(date_max) if date_max else max_dt
        new_min     = str(date_min) if date_min else min_dt
        
    except Exception as e:
        return (dbc.Alert(f"❌ Upload pipeline error: {e}", color="danger"), no_update, no_update, no_update, no_update, no_update, no_update)

    # ── Stage 5: Build summary UI ────────────────────────────────────────
    warn_items = [dbc.Alert(w, color="warning", className="py-1 px-2 small") for w in warnings]
    summary = dbc.Table([
        html.Thead(html.Tr([html.Th("Field"), html.Th("Value")])),
        html.Tbody([
            html.Tr([html.Td("File"),              html.Td(filename)]),
            html.Tr([html.Td("Rows in upload"),    html.Td(f"{len(raw_df):,}")]),
            html.Tr([html.Td("New rows inserted"), html.Td(html.Span(f"{n_inserted:,}", className="text-success fw-bold"))]),
            html.Tr([html.Td("Duplicates skipped"),html.Td(f"{n_dupes:,}")]),
            html.Tr([html.Td("DB rows before"),    html.Td(f"{rows_before:,}")]),
            html.Tr([html.Td("DB rows after"),     html.Td(f"{rows_after:,}")]),
            html.Tr([html.Td("Date range"),        html.Td(f"{date_min} → {date_max}")]),
        ])
    ], bordered=True, hover=True, size="sm", className="mt-3")

    status = html.Div([
        dbc.Alert(f"✅ Upload complete — {n_inserted:,} new rows added, {n_dupes:,} duplicates skipped.",
                  color="success"),
        *warn_items,
        summary,
    ])

    db_stats_data = {"rows": rows_after, "date_min": str(date_min), "date_max": str(date_max)}
    new_version   = (current_version or 0) + 1
    invalidate_hierarchy_cache()
    logger.info("CACHE INVALIDATED after upload")

    return status, db_stats_data, new_version, new_min, new_max, new_min, new_max

@app.callback(
    Output("store-db-stats", "data", allow_duplicate=True),
    Input("store-admin-auth", "data"),
    prevent_initial_call=True,
)
def load_db_stats_on_login(is_auth):
    if not is_auth:
        return no_update
    import sqlite3
    try:
        con = sqlite3.connect(DB_PATH)
        rows  = pd.read_sql("SELECT COUNT(*) as n FROM interruption_events", con).iloc[0]["n"]
        dates = pd.read_sql("SELECT MIN(event_date) as mn, MAX(event_date) as mx FROM interruption_events", con).iloc[0]
        con.close()
        return {"rows": int(rows), "date_min": str(dates["mn"]), "date_max": str(dates["mx"])}
    except Exception as e:
        return {"error": str(e)}

@app.callback(
    Output("admin-db-stats", "children"),
    Input("store-db-stats",  "data"),
)
def render_db_stats(data):
    """Single renderer: store-db-stats → admin-db-stats div."""
    if not data:
        return no_update
    if "error" in data:
        return dbc.Alert(f"Could not load DB stats: {data['error']}", color="warning", className="small")
    return dbc.Alert(
        f"📦 interruption_events: {data['rows']:,} rows  |  "
        f"Date range: {data['date_min']} → {data['date_max']}",
        color="info", className="small",
    )



# ── Entry point ───────────────────────────────────────────────────────────────
# ── Rollback callbacks ────────────────────────────────────────────────────────

from dash import ALL, ctx
from delta_load import rollback_upload
from db import get_recent_uploads


def _upload_card(u: dict) -> dbc.Card:
    """Render one upload as a Bootstrap Card with a Rollback button."""
    uid      = u["upload_id"]
    ts       = u["upload_ts"][:19].replace("T", "  ")
    fname    = u["filename"]
    inserted = u["inserted"]
    skipped  = u["skipped"]
    return dbc.Card(
        dbc.CardBody([
            html.Div([
                html.Div([
                    html.Strong(f"📅  {ts}", className="d-block"),
                    html.Small(fname, className="text-muted"),
                ], className="flex-grow-1"),
                html.Div([
                    html.Span(f"+{inserted:,} rows",  className="badge bg-success me-1"),
                    html.Span(f"{skipped:,} skipped", className="badge bg-secondary me-2"),
                    dbc.Button(
                        "↩ Rollback",
                        id={"type": "rollback-btn", "index": uid},
                        color="danger",
                        outline=True,
                        size="sm",
                    ),
                ], className="d-flex align-items-center"),
            ], className="d-flex justify-content-between align-items-center"),
        ]),
        className="mb-2 shadow-sm",
    )


@app.callback(
    Output("rollback-cards", "children"),
    Input("store-admin-auth",        "data"),
    Input("rollback-result",         "children"),
    Input("admin-upload-status",     "children"),
    prevent_initial_call=False,
)
def load_rollback_cards(is_auth, _result, _upload_status):
    """Populate the recent-uploads list on load and after each rollback."""
    if not is_auth:
        return html.P("Please log in to view upload history.", className="text-muted small")
    try:
        uploads = get_recent_uploads(limit=MAX_ROLLBACK_VISIBLE)
    except Exception as e:
        return dbc.Alert(f"Could not load upload log: {e}", color="warning", className="small")
    if not uploads:
        return html.P(
            "No tracked uploads yet. Uploads made after running db_rollback_migration.py will appear here.",
            className="text-muted small",
        )
    return [_upload_card(u) for u in uploads]


@app.callback(
    Output("rollback-confirm-modal", "is_open"),
    Output("rollback-modal-body",    "children"),
    Output("rollback-pending-id",    "data"),
    Input({"type": "rollback-btn", "index": ALL}, "n_clicks"),
    State("store-admin-auth", "data"),
    prevent_initial_call=True,
)
def open_rollback_modal(n_clicks_list, is_auth):
    """Open confirmation modal when a Rollback button is clicked."""
    if not is_auth or not any(n for n in n_clicks_list if n):
        return False, no_update, no_update

    triggered = ctx.triggered_id
    if triggered is None or not isinstance(triggered, dict):
        return False, no_update, no_update

    upload_id = triggered["index"]

    # Fetch the upload record for display
    try:
        uploads = get_recent_uploads(limit=MAX_ROLLBACK_VISIBLE)
        record  = next((u for u in uploads if u["upload_id"] == upload_id), None)
    except Exception:
        record = None

    if record:
        ts    = record["upload_ts"][:19].replace("T", "  ")
        fname = record["filename"]
        body  = html.Div([
            html.P([html.Strong("Upload date: "), ts]),
            html.P([html.Strong("File: "), html.Code(fname)]),
            html.P([
                html.Strong("Rows that will be deleted: "),
                html.Span(f"{record['inserted']:,}", className="text-danger fw-bold"),
            ]),
            dbc.Alert(
                "⚠️ This action is irreversible. All rows inserted by this upload will be "
                "permanently removed and the daily summary will be recalculated.",
                color="warning", className="small mt-2",
            ),
        ])
    else:
        body = html.P(f"Confirm rollback of upload #{upload_id}?")

    return True, body, upload_id


@app.callback(
    Output("rollback-confirm-modal", "is_open", allow_duplicate=True),
    Output("rollback-pending-id",    "data",    allow_duplicate=True),
    Input("rollback-cancel-btn", "n_clicks"),
    prevent_initial_call=True,
)
def cancel_rollback(_n):
    """Close the modal and clear the pending upload id."""
    return False, None


@app.callback(
    Output("rollback-result",        "children"),
    Output("rollback-confirm-modal", "is_open",          allow_duplicate=True),
    Output("rollback-pending-id",    "data",             allow_duplicate=True),
    Output("store-db-stats",         "data",             allow_duplicate=True),
    Output("store-data-version",     "data",             allow_duplicate=True),
    Output("global-date-range",      "min_date_allowed", allow_duplicate=True),
    Output("global-date-range",      "max_date_allowed", allow_duplicate=True),
    Output("global-date-range",      "start_date",       allow_duplicate=True),
    Output("global-date-range",      "end_date",         allow_duplicate=True),
    Input("rollback-confirm-btn",    "n_clicks"),
    State("rollback-pending-id",     "data"),
    State("store-admin-auth",        "data"),
    State("store-data-version",      "data"),
    prevent_initial_call=True,
)
def execute_rollback(_n, upload_id, is_auth, current_version):
    """Execute the rollback atomically and refresh UI."""
    if not is_auth:
        return dbc.Alert("⚠️ Not authenticated.", color="warning"), False, None, no_update, no_update, no_update, no_update, no_update, no_update
    if upload_id is None:
        return dbc.Alert("⚠️ No upload selected.", color="warning"), False, None, no_update, no_update, no_update, no_update, no_update, no_update

    result = rollback_upload(upload_id, db_path=DB_PATH)

    if "error" in result:
        err = result["error"]
        if "locked" in err.lower() or "busy" in err.lower():
            msg = dbc.Alert(
                f"❌ Database is busy (concurrent access). Please retry in a moment.\n{err}",
                color="danger",
            )
        else:
            msg = dbc.Alert(f"❌ Rollback failed: {err}", color="danger")
        return msg, False, None, no_update, no_update, no_update, no_update, no_update, no_update

    deleted = result["deleted"]
    msg = dbc.Alert(
        f"✅ Rollback complete — {deleted:,} row(s) removed for upload #{upload_id}.",
        color="success",
    )

    # Refresh DB stats and trigger global data refresh
    try:
        import sqlite3
        con   = sqlite3.connect(DB_PATH)
        rows  = pd.read_sql("SELECT COUNT(*) as n FROM interruption_events", con).iloc[0]["n"]
        dates = pd.read_sql(
            "SELECT MIN(event_date) as mn, MAX(event_date) as mx FROM interruption_events", con
        ).iloc[0]
        con.close()
        new_stats = {"rows": int(rows), "date_min": str(dates["mn"]), "date_max": str(dates["mx"])}
    except Exception:
        new_stats = no_update

    new_version = (current_version or 0) + 1
    db_min, db_max = _get_db_date_bounds()
    invalidate_hierarchy_cache()
    logger.info("CACHE INVALIDATED after rollback")
    return msg, False, None, new_stats, new_version, db_min, db_max, db_min, db_max

#Landing Page Layout Changes STARTS
_SECTION_META = {
    "tab-overview": ("region-overview", "Overview"),
    "tab-ex":       ("region-ex",        "Interruption Explorer"),
    "tab-trends":   ("region-trends", "Trends & Comparison"),
    "tab-admin":    ("region-admin",    "Admin"),
    
}

# Card clicks → store-active-tab
@app.callback(
    Output("store-active-tab", "data"),
    [
        Input("nav-card-tab-overview", "n_clicks"),
        Input("nav-card-tab-ex",        "n_clicks"),
        Input("nav-card-tab-trends",    "n_clicks"),
        Input("nav-card-tab-admin",    "n_clicks"),
        Input("back-strip",            "n_clicks"),
    ],
    prevent_initial_call=True,
)

def _set_active_tab(ov, ex, tr, ad, back):
    tid = ctx.triggered_id
    if tid == "back-strip":
        return "landing"
    mapping = {
        "nav-card-tab-overview":  "tab-overview",
        "nav-card-tab-ex":        "tab-ex",
        "nav-card-tab-trends":    "tab-trends",
        "nav-card-tab-admin":     "tab-admin",
    }
    return mapping.get(tid, "landing")
# store-active-tab → show/hide regions + sync hidden dbc.Tabs
@app.callback(
    Output("region-landing",  "style"),
    Output("region-overview", "style"),
    Output("region-ex",        "style"),
    Output("region-trends", "style"),
    Output("region-admin",    "style"),
    Output("back-strip",      "style"),
    Output("back-strip-section", "children"),
    Output("tabs",            "active_tab"),
    Input("store-active-tab", "data"),
)

def _route_sections(active):
    hidden   = {"display": "none"}
    visible  = {"display": "block"}
    show_back = {
        "display": "flex", "alignItems": "center", "gap": "0.5rem",
        "padding": "0.6rem 1rem", "background": "#f0f9ff",
        "borderBottom": "1px solid #cde4f5",
        "fontSize": "0.82rem", "color": "#0a2540",
        "cursor": "pointer",
    }

    # defaults
    land = ov = ex = tr = ad = hidden
    back_style   = hidden
    back_label   = ""
    active_tab   = "tab-overview"

    if active == "landing":
        land = visible
    elif active in _SECTION_META:
        region_id, label = _SECTION_META[active]
        region_styles = {
            "tab-overview": lambda: setattr(locals(), "ov", visible),
            "tab-ex":       lambda: setattr(locals(), "ex", visible),
            "tab-trends": lambda: setattr(locals(), "tr",   visible),
            "tab-admin":    lambda: setattr(locals(), "ad", visible),
        }
        # Direct assignment (lambdas above won't work in a closure, use if/elif)
        if   active == "tab-overview": ov = visible
        elif active == "tab-ex":        ex = visible
        elif active == "tab-trends":    tr = visible
        elif active == "tab-admin":    ad = visible
        back_style = show_back
        back_label = f" {label}"
        active_tab = active

    return land, ov, ex, tr, ad, back_style, back_label, active_tab
#Landing Page Layout Changes ENDS

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=DEBUG, host=HOST, port=PORT)
