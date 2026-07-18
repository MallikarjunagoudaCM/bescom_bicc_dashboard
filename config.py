"""
config.py
---------
Central configuration for the BICC Dash application.

All environment variables are read here. The rest of the app
imports from this module — never reads os.environ directly.

Local development:  copy .env.example → .env and fill in values.
Azure App Service:  set these as Application Settings (Settings → Configuration).
Plotly Cloud:       set these as Environment Variables in the deployment settings.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env file when present (local dev only).
# In production (Azure / Plotly Cloud) env vars are injected directly
# and python-dotenv simply finds no .env file — no error.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed — rely on real env vars


# ── Database ──────────────────────────────────────────────────────────────────

# Absolute or relative path to the SQLite database file.
# In production point this to a persistent volume / mounted path.
DB_PATH: str = os.environ.get("DB_PATH", "bicc.db")

# ── Admin credentials ─────────────────────────────────────────────────────────

# NEVER hard-code these. Set them in .env (local) or App Settings (Azure).
ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_USERNAME and ADMIN_PASSWORD must be set via .env or environment "
        "variables. No default credentials are provided for security reasons. "
        "Copy .env.example to .env and set real values."
    )

# ── UI limits ─────────────────────────────────────────────────────────────────

# Maximum number of days selectable in the date-range picker (Overview tab).
MAX_OVERVIEW_DAYS: int = int(os.environ.get("MAX_OVERVIEW_DAYS", "90"))

# Maximum rows returned to the DataTable (prevents browser slowdown).
MAX_TABLE_ROWS_UI: int = int(os.environ.get("MAX_TABLE_ROWS_UI", "10000"))

# Maximum rollback entries shown in the Admin tab rollback panel.
MAX_ROLLBACK_VISIBLE: int = int(os.environ.get("MAX_ROLLBACK_VISIBLE", "5"))

# Anomaly detection: flag daily outage counts above this many std-deviations
TREND_ANOMALY_ZSCORE: float = float(os.environ.get("TREND_ANOMALY_ZSCORE", "2.0"))

# ── KPI Colour Thresholds ─────────────────────────────────────────────────────
KPI_EVENTS_WARN   = 100
KPI_EVENTS_DANGER = 200

KPI_AVG_EVENTS_WARN   = 20
KPI_AVG_EVENTS_DANGER = 40

KPI_CUST_WARN   = 50_000
KPI_CUST_DANGER = 150_000

KPI_CMI_WARN   = 500_000
KPI_CMI_DANGER = 2_000_000

KPI_UNSCHED_WARN   = 60.0
KPI_UNSCHED_DANGER = 80.0

KPI_MINS_WARN   = 5_000
KPI_MINS_DANGER = 15_000

# Total consumer base — set this in .env or Azure App Settings
TOTAL_CUSTOMERS: int = int(os.environ.get("TOTAL_CUSTOMERS", "6307034"))

# ── Server ────────────────────────────────────────────────────────────────────

DEBUG: bool = os.environ.get("DASH_DEBUG", "false").lower() == "true"
HOST: str   = os.environ.get("HOST", "0.0.0.0")
PORT: int   = int(os.environ.get("PORT", "8050"))

# SAIDI = CMI / TOTAL_CUSTOMERS (minutes per customer)
KPI_SAIDI_WARN:   float = float(os.environ.get("KPI_SAIDI_WARN",   "1.0"))
KPI_SAIDI_DANGER: float = float(os.environ.get("KPI_SAIDI_DANGER", "3.0"))
KPI_SAIDI_WARN_CC: float = float(os.environ.get("KPI_SAIDI_WARN",   "1.0"))
KPI_SAIDI_DANGER_CC: float = float(os.environ.get("KPI_SAIDI_WARN",   "2.0"))

# SAIFI = Outages / TOTAL_CUSTOMERS (minutes per customer)
KPI_SAIFI_WARN      = 1.0   # adjust to your grid standard
KPI_SAIFI_DANGER    = 3.0
KPI_SAIFI_WARN_CC    = 2.0
KPI_SAIFI_DANGER_CC  = 4.0
