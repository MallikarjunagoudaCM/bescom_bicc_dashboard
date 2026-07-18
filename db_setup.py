"""
db_setup.py
-----------
Seeds bicc.db from:
- SDO_HIERARCHY_MASTER.xlsx
- STATION_FEEDER_MASTER.xlsx
- interruption_data.xlsx

Changes vs. previous version:
  - _rename_interruption_columns: added 'customers_affected' and 'cmi' rules
  - seed(): numeric coercion for customers_affected + cmi
  - daily_summary now includes total_customers_affected and total_cmi columns
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
DB = BASE / "bicc.db"


def _pick_first_existing(*names: str) -> Path:
    for name in names:
        p = BASE / name
        if p.exists():
            return p
    raise FileNotFoundError(f"None of these files were found: {names}")


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _find_detail_sheet(xlsx_path: Path) -> pd.DataFrame:
    xl = pd.ExcelFile(xlsx_path)
    for sheet in xl.sheet_names:
        df = pd.read_excel(xlsx_path, sheet_name=sheet)
        probe = df.dropna(how="all")
        if probe.empty:
            continue
        probe = probe.reset_index(drop=True)
        first_val = str(probe.iloc[0, 0]).strip()
        if probe.shape[1] >= 10 and first_val.replace(".", "", 1).isdigit():
            return _clean_columns(df)
    return _clean_columns(pd.read_excel(xlsx_path, sheet_name=xl.sheet_names[-1]))


def _rename_interruption_columns(detail: pd.DataFrame) -> pd.DataFrame:
    detail = detail.copy()

    target_rules = [
        ("sl_no",               lambda lc: "sl" in lc and "no" in lc),
        ("subdivision",         lambda lc: "sub" in lc and "div" in lc),
        ("division",            lambda lc: lc == "division"),
        ("station",             lambda lc: lc == "station"),
        ("feeder_type",         lambda lc: "feeder" in lc and "type" in lc),
        ("feeder",              lambda lc: lc == "feeder"),
        ("site_code",           lambda lc: "site" in lc and "code" in lc),
        ("hrn",                 lambda lc: "hrn" in lc or ("equip" in lc and "(" in lc)),
        ("trouble_dt",          lambda lc: "trouble" in lc),
        ("close_dt",            lambda lc: "close" in lc and ("date" in lc or "time" in lc)),
        ("duration_mins",       lambda lc: "duration" in lc),
        ("work_performed",      lambda lc: "work" in lc and "perf" in lc),
        ("cause",               lambda lc: lc == "cause"),
        ("outage_type",         lambda lc: "outage" in lc and "type" in lc),
        ("trip_status",         lambda lc: "tripped" in lc or "operated" in lc),
        ("agency",              lambda lc: "kptcl" in lc or "bescom" in lc),
        ("level",               lambda lc: "equip" in lc and "feeder" in lc),
        # NOTE: outage_id rule must come AFTER outage_type to avoid false match
        ("outage_id",           lambda lc: "outage" in lc and "id" in lc),
        # ── NEW columns ──────────────────────────────────────────────────────
        ("customers_affected",  lambda lc: "customer" in lc and "affected" in lc),
        ("cmi",                 lambda lc: lc == "cmi" or ("cust" in lc and "minute" in lc)),
    ]

    col_map = {}
    used_targets = set()
    for c in detail.columns:
        lc = str(c).strip().lower()
        for target, rule in target_rules:
            if target not in used_targets and rule(lc):
                col_map[c] = target
                used_targets.add(target)
                break

    detail = detail.rename(columns=col_map)
    detail = detail.loc[:, ~detail.columns.duplicated()].copy()
    return detail
    
    
def seed_customer_masters() -> None:
    """
    Seed division_customers and feeder_customers tables from XLSX files.
    Expected files:
      - DIVISION_CUSTOMERS.xlsx  → columns: Division, Customers
      - FEEDER_CUSTOMERS.xlsx    → columns: Station, Feeder, Customers
    """
    con = sqlite3.connect(DB)
    try:
        # ── Division customers ────────────────────────────────────────
        div_path = BASE / "DIVISION_CUSTOMERS.xlsx"
        if div_path.exists():
            df_div = pd.read_excel(div_path)
            df_div.columns = [c.strip() for c in df_div.columns]
            df_div = df_div.rename(columns={
                "Division":        "division",
                "Customers": "customers",
            })
            df_div = df_div[["division", "customers"]].dropna()
            df_div["customers"] = pd.to_numeric(
                df_div["customers"], errors="coerce"
            ).fillna(0).astype(int)
            df_div.to_sql("division_customers", con,
                          if_exists="replace", index=False)
            print(f"division_customers seeded — {len(df_div)} rows")
        else:
            print("DIVISION_CUSTOMERS.xlsx not found — skipped")

        # ── Feeder customers ──────────────────────────────────────────
        fdr_path = BASE / "FEEDER_CUSTOMERS.xlsx"
        if fdr_path.exists():
            df_fdr = pd.read_excel(fdr_path)
            df_fdr.columns = [c.strip() for c in df_fdr.columns]
            df_fdr = df_fdr.rename(columns={
                "Station":         "station",
                "Feeder":          "feeder",
                "Customers": "customers",
            })
            df_fdr = df_fdr[["station", "feeder", "customers"]].dropna()
            df_fdr["customers"] = pd.to_numeric(
                df_fdr["customers"], errors="coerce"
            ).fillna(0).astype(int)
            df_fdr.to_sql("feeder_customers", con,
                          if_exists="replace", index=False)
            print(f"feeder_customers seeded — {len(df_fdr)} rows")
        else:
            print("FEEDER_CUSTOMERS.xlsx not found — skipped")

        con.commit()
    finally:
        con.close()    


def seed() -> None:
    sdo_path          = _pick_first_existing("SDO_HIERARCHY_MASTER.xlsx")
    sf_path           = _pick_first_existing("STATION_FEEDER_MASTER.xlsx")
    interruption_path = _pick_first_existing("interruption_data.xlsx", "interruption_data_1.xlsx")

    con = sqlite3.connect(DB)
    try:
        # ── SDO hierarchy ────────────────────────────────────────────────────
        sdo = pd.read_excel(sdo_path)
        sdo.columns = [str(c).strip().upper() for c in sdo.columns]
        sdo = sdo.rename(columns={
            "SUBDIVISION": "subdivision",
            "DIVISION":    "division",
            "CIRCLE":      "circle",
            "ZONE":        "zone",
        })
        sdo.to_sql("sdo_hierarchy", con, if_exists="replace", index=False)

        # ── Station / feeder master ──────────────────────────────────────────
        sfm = pd.read_excel(sf_path)
        sfm = _clean_columns(sfm)
        sfm = sfm.rename(columns={
            "SL NO":           "sl_no",
            "Control Center":  "control_center",
            "Division":        "division",
            "Station":         "station",
            "Feeder":          "feeder",
            "Feeder Type":     "feeder_type",
            "Feeder Category": "feeder_category",
        })
        sfm.to_sql("station_feeder", con, if_exists="replace", index=False)

        # ── Interruption events ──────────────────────────────────────────────
        detail = _find_detail_sheet(interruption_path)
        detail = _rename_interruption_columns(detail)

        for dcol in ["trouble_dt", "close_dt"]:
            if dcol in detail.columns:
                detail[dcol] = pd.to_datetime(detail[dcol], errors="coerce", dayfirst=True)

        if "trouble_dt" in detail.columns:
            detail["event_date"] = detail["trouble_dt"].dt.date.astype(str)

        # Existing numeric column
        if "duration_mins" in detail.columns:
            detail["duration_mins"] = pd.to_numeric(detail["duration_mins"], errors="coerce")

        # ── NEW: coerce the two new numeric columns ──────────────────────────
        for num_col in ("customers_affected", "cmi"):
            if num_col in detail.columns:
                detail[num_col] = pd.to_numeric(detail[num_col], errors="coerce")

        # Text columns (outage_id kept as text; new numerics excluded)
        text_cols = [
            "subdivision", "division", "station", "feeder", "feeder_type",
            "site_code", "hrn", "work_performed", "cause", "outage_type",
            "trip_status", "agency", "level", "outage_id",
        ]
        for col in text_cols:
            if col in detail.columns:
                detail[col] = detail[col].astype(str).replace({"nan": None, "NaT": None})

        detail.to_sql("interruption_events", con, if_exists="replace", index=False)

        # ── daily_summary — extended with CMI aggregates ─────────────────────
        if "event_date" in detail.columns and "duration_mins" in detail.columns:
            count_col = "sl_no" if "sl_no" in detail.columns else detail.columns[0]

            agg_spec = {
                "total_events":              (count_col,          "count"),
                "total_duration":            ("duration_mins",     "sum"),
            }
            # Only aggregate new columns if they were present in the xlsx
            if "customers_affected" in detail.columns:
                agg_spec["total_customers_affected"] = ("customers_affected", "sum")
            if "cmi" in detail.columns:
                agg_spec["total_cmi"] = ("cmi", "sum")

            daily = (
                detail
                .groupby("event_date", dropna=False)
                .agg(**agg_spec)
                .reset_index()
            )
            daily["avg_duration"] = (
                daily["total_duration"] / daily["total_events"]
            ).round(2)

            # Round new aggregates for readability
            if "total_customers_affected" in daily.columns:
                daily["total_customers_affected"] = (
                    daily["total_customers_affected"].round(0).astype("Int64")
                )
            if "total_cmi" in daily.columns:
                daily["total_cmi"] = daily["total_cmi"].round(2)

            daily.to_sql("daily_summary", con, if_exists="replace", index=False)

        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_ie_eventdate    ON interruption_events(event_date)",
            "CREATE INDEX IF NOT EXISTS idx_ie_feeder       ON interruption_events(feeder)",
            "CREATE INDEX IF NOT EXISTS idx_ie_station      ON interruption_events(station)",
            "CREATE INDEX IF NOT EXISTS idx_ie_division     ON interruption_events(division)",
            "CREATE INDEX IF NOT EXISTS idx_ie_troubledt    ON interruption_events(trouble_dt)",
            "CREATE INDEX IF NOT EXISTS idx_ie_date_feeder  ON interruption_events(event_date, feeder)",
            "CREATE INDEX IF NOT EXISTS idx_sf_cc_div       ON station_feeder(control_center, division)",
            "CREATE INDEX IF NOT EXISTS idx_sf_feeder_stn   ON station_feeder(feeder, station)",
            "CREATE INDEX IF NOT EXISTS idx_ie_agency       ON interruption_events(agency)",
        ]
        for sql in indexes:
            con.execute(sql)
        con.commit()
        print(f"Database seeded successfully -> {DB.resolve()}")
    finally:
        con.close()
if __name__ == "__main__":
    seed()
    seed_customer_masters() # ← run both

