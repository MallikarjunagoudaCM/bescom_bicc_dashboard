"""
delta_load.py
-------------
Upload-aware delta loader for BESCOM BICC Dashboard.

Extends the original delta_load.py logic with:
  - Upload tracking via delta_upload_log table (upload_id per batch)
  - upload_id stamped on every inserted row in interruption_events
  - rollback_upload() to atomically delete a batch and rebuild daily_summary

Column rules, key logic, and daily_summary schema are identical to the
original delta_load.py / db_setup.py so existing data is fully compatible.

daily_summary columns: event_date, total_events, total_duration, avg_duration

Public API (called from app_fixed_no_limit.py Admin tab):
    result = delta_insert_tracked(file_bytes, filename, db_path)
    result = rollback_upload(upload_id, db_path)
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path(__file__).parent / "bicc.db"

COLUMN_RULES = [
    ("sl_no", lambda c: "sl" in c and "no" in c),
    ("subdivision", lambda c: "sub" in c and "div" in c),
    ("division", lambda c: c == "division"),
    ("station", lambda c: c == "station"),
    ("feeder_type", lambda c: "feeder" in c and "type" in c),
    ("feeder", lambda c: c == "feeder"),
    ("site_code", lambda c: "site" in c and "code" in c),
    ("hrn", lambda c: "hrn" in c or ("equip" in c and "(" in c)),
    ("trouble_dt", lambda c: "trouble" in c),
    ("close_dt", lambda c: "close" in c and ("date" in c or "time" in c)),
    ("duration_mins", lambda c: "duration" in c),
    ("work_performed", lambda c: "work" in c and "perf" in c),
    ("cause", lambda c: c == "cause"),
    ("outage_type", lambda c: "outage" in c and "type" in c),
    ("trip_status", lambda c: "tripped" in c or "operated" in c),
    ("agency", lambda c: "kptcl" in c or "bescom" in c),
    ("level", lambda c: "equip" in c and "feeder" in c),
    ("outage_id", lambda c: "outage" in c and "id" in c),
    ("customers_affected", lambda lc: "customer" in lc and "affected" in lc),
    ("cmi", lambda lc: "cmi" in lc or ("cust" in lc and "minutes" in lc)),
]

def _read_excel(file_bytes: bytes) -> pd.DataFrame:
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    df = None
    for sheet in xl.sheet_names:
        candidate = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet)
        candidate.columns = [str(c).strip() for c in candidate.columns]
        probe = candidate.dropna(how="all").reset_index(drop=True)
        if probe.shape[1] >= 10 and str(probe.iloc[0, 0]).replace(".", "").isdigit():
            df = candidate
            break
    if df is None:
        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=xl.sheet_names[-1])
    df.columns = [str(c).strip() for c in df.columns]
    return df

def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map, used = {}, set()
    for col in df.columns:
        lc = col.strip().lower()
        for target, rule in COLUMN_RULES:
            if target not in used and rule(lc):
                col_map[col] = target
                used.add(target)
                break
    df = df.rename(columns=col_map)
    df = df.loc[:, ~df.columns.duplicated()]
    return df

def _convert_types(df: pd.DataFrame) -> pd.DataFrame:
    for dcol in ["trouble_dt", "close_dt"]:
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce", dayfirst=True)

    if "trouble_dt" in df.columns:
        df["event_date"] = df["trouble_dt"].dt.date.astype(str)

    if "duration_mins" in df.columns:
        df["duration_mins"] = pd.to_numeric(df["duration_mins"], errors="coerce")
        
    # NEW: numeric metrics
    for num_col in ["customers_affected", "cmi"]:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce")

    text_cols = [
        "subdivision", "division", "station", "feeder", "feeder_type",
        "site_code", "hrn", "work_performed", "cause", "outage_type",
        "trip_status", "agency", "level", "outage_id",
    ]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).replace({"nan": None, "NaT": None})
    return df

def _build_delta_key(df: pd.DataFrame) -> pd.DataFrame:
    df["_delta_key"] = (
        df.get("event_date", pd.Series("", index=df.index)).astype(str) + "|" +
        df.get("feeder", pd.Series("", index=df.index)).fillna("").astype(str) + "|" +
        df.get("trouble_dt", pd.Series("", index=df.index)).astype(str)
    )
    return df

def _rebuild_daily_summary(con: sqlite3.Connection) -> None:
    full = pd.read_sql(
        "SELECT event_date, duration_mins FROM interruption_events", con
    )

    con.execute("DELETE FROM daily_summary")

    if full.empty:
        return

    count_series = full.groupby("event_date")["duration_mins"].count()
    sum_series = full.groupby("event_date")["duration_mins"].sum()

    daily = pd.DataFrame({
        "event_date": count_series.index,
        "total_events": count_series.values,
        "total_duration": sum_series.values,
    })
    daily["avg_duration"] = (
        daily["total_duration"] / daily["total_events"].replace(0, 1)
    ).round(2)

    daily.to_sql("daily_summary", con, if_exists="append", index=False)

def delta_insert_tracked(
    file_bytes: bytes,
    filename: str,
    db_path: Path = DEFAULT_DB,
) -> dict:
    upload_ts = datetime.now(timezone.utc).isoformat()

    try:
        df = _read_excel(file_bytes)
    except Exception as exc:
        return {"error": f"Could not read workbook: {exc}"}

    df = _normalise_columns(df)
    df = _convert_types(df)

    if "feeder" not in df.columns or "trouble_dt" not in df.columns:
        return {
            "error": "Validation failed: required columns (feeder, trouble_dt) not found after normalisation.\n"
                     f"Detected columns: {list(df.columns)}"
        }

    df = _build_delta_key(df)

    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=15000;")

    try:
        rows_before = con.execute(
            "SELECT COUNT(*) FROM interruption_events"
        ).fetchone()[0]

        try:
            existing = pd.read_sql(
                "SELECT event_date, feeder, trouble_dt FROM interruption_events", con
            )
            existing["_delta_key"] = (
                existing.get("event_date", pd.Series("", index=existing.index)).astype(str) + "|" +
                existing.get("feeder", pd.Series("", index=existing.index)).fillna("").astype(str) + "|" +
                existing.get("trouble_dt", pd.Series("", index=existing.index)).astype(str)
            )
            existing_keys = set(existing["_delta_key"].dropna().astype(str))
        except Exception:
            existing_keys = set()

        new_df = df[~df["_delta_key"].isin(existing_keys)].copy()
        inserted = len(new_df)
        skipped = len(df) - inserted

        cur = con.execute(
            """
            INSERT INTO delta_upload_log (upload_ts, filename, inserted, skipped, status)
            VALUES (?, ?, ?, ?, 'success')
            """,
            (upload_ts, filename, inserted, skipped),
        )
        upload_id = cur.lastrowid

        if inserted > 0:
            new_df = new_df.drop(columns=["_delta_key"], errors="ignore")
            new_df["upload_id"] = upload_id
            new_df.to_sql("interruption_events", con, if_exists="append", index=False)

        _rebuild_daily_summary(con)

        rows_after = con.execute(
            "SELECT COUNT(*) FROM interruption_events"
        ).fetchone()[0]
        date_row = con.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM interruption_events"
        ).fetchone()
        date_min, date_max = (date_row[0] or ""), (date_row[1] or "")

        con.commit()

        return {
            "inserted": inserted,
            "skipped": skipped,
            "rows_before": rows_before,
            "rows_after": rows_after,
            "date_min": str(date_min),
            "date_max": str(date_max),
            "upload_id": upload_id,
        }

    except sqlite3.OperationalError as exc:
        con.rollback()
        err = str(exc)
        if "locked" in err or "busy" in err:
            return {"error": "Database is currently busy with another transaction. Please retry in a few seconds."}
        return {"error": f"Database error during insert: {exc}"}

    except Exception as exc:
        con.rollback()
        return {"error": f"Unexpected error during insert: {exc}"}

    finally:
        con.close()

def rollback_upload(upload_id: int, db_path: Path = DEFAULT_DB) -> dict:
    if not isinstance(upload_id, int) or upload_id <= 0:
        return {"error": "Invalid upload_id."}

    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=15000;")

    try:
        log_row = con.execute(
            "SELECT upload_id, status, inserted FROM delta_upload_log WHERE upload_id=?",
            (upload_id,),
        ).fetchone()

        if log_row is None:
            return {"error": f"Upload #{upload_id} not found in log."}

        if log_row[1] == "rolled_back":
            return {"error": f"Upload #{upload_id} has already been rolled back."}

        cur = con.execute(
            "DELETE FROM interruption_events WHERE upload_id = ?",
            (upload_id,),
        )
        deleted = cur.rowcount

        con.execute(
            "UPDATE delta_upload_log SET status='rolled_back' WHERE upload_id=?",
            (upload_id,),
        )

        _rebuild_daily_summary(con)
        con.commit()

        return {"deleted": deleted, "upload_id": upload_id}

    except sqlite3.OperationalError as exc:
        con.rollback()
        err = str(exc)
        if "locked" in err or "busy" in err:
            return {
                "error": (
                    "Database is locked by another concurrent transaction. "
                    "The rollback was not applied — please retry in a few seconds."
                )
            }
        return {"error": f"Database error during rollback: {exc}"}

    except Exception as exc:
        con.rollback()
        return {"error": f"Unexpected rollback error: {exc}"}

    finally:
        con.close()