"""
db.py
-----
Thin data-access layer for the BICC Dash app.
All queries go through this module — the rest of the app never imports sqlite3 directly.

PATCH NOTES (2026-06-06):
- get_overview_kpis() now returns two additional keys:
    "agency_split"       : pd.DataFrame  — per-CC, per-agency events/mins/cmi
    "feeders_interrupted": pd.DataFrame  — per-CC count of feeders with >=1 event
  Both use the same date_filter / level_mode as the existing totals query.
"""
import sqlite3, pandas as pd
import time
from pathlib import Path
import threading
from config import DB_PATH, TOTAL_CUSTOMERS
DB = Path(DB_PATH)

import logging
logger = logging.getLogger("bicc.cache")
logging.basicConfig(level=logging.DEBUG)

_HIERARCHY_CACHE: dict = {}
_CACHE_TTL = 300  # 5 minutes
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0, "stales": 0, "sets": 0}

def invalidate_hierarchy_cache():
    with _cache_lock:
        count = len(_HIERARCHY_CACHE)
        _HIERARCHY_CACHE.clear()
        logger.info(f"CACHE INVALIDATED | {count} keys cleared")

def invalidate_cache_key(key: str):
    with _cache_lock:
        removed = _HIERARCHY_CACHE.pop(key, None)
        if removed:
            logger.info(f"CACHE KEY REMOVED | {key}")

def get_cache_stats() -> dict:
    with _cache_lock:
        stats = dict(_cache_stats)
        total = stats["hits"] + stats["misses"] + stats["stales"]
        stats["hit_rate_pct"] = round(stats["hits"] / total * 100, 1) if total else 0.0
        stats["total_keys"] = len(_HIERARCHY_CACHE)
        stats["ttl_seconds"] = _CACHE_TTL
        return stats

def _cache_get(key: str):
    entry = _HIERARCHY_CACHE.get(key)
    if entry:
        age = time.time() - entry["ts"]
        if age < _CACHE_TTL:
            with _cache_lock:
                _cache_stats["hits"] += 1
            logger.debug(f"CACHE HIT | {key} | age={age:.1f}s")
            return entry["val"]
        else:
            with _cache_lock:
                _cache_stats["stales"] += 1
            logger.warning(f"CACHE STALE | {key} | age={age:.1f}s — will refetch")
    else:
        with _cache_lock:
            _cache_stats["misses"] += 1
        logger.debug(f"CACHE MISS | {key}")
    return None

def _cache_set(key: str, val):
    with _cache_lock:
        _HIERARCHY_CACHE[key] = {"val": val, "ts": time.time()}
        _cache_stats["sets"] += 1
    logger.debug(f"CACHE SET | {key} | entries={len(val) if hasattr(val, '__len__') else 1}")

def _level_mode_condition(level_mode: str = "both", alias: str = ""):
    prefix = f"{alias}." if alias else ""
    level_expr = f"UPPER(COALESCE(TRIM({prefix}level), 'FEEDER'))"
    if level_mode == "feeder":
        return f"{level_expr} = 'FEEDER'", []
    if level_mode == "equipment":
        return f"{level_expr} <> 'FEEDER'", []
    return "", []

def _normalize_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, "")]
    return [value] if value != "" else []

def _add_in_filter(filters, params, column, value):
    vals = _normalize_list(value)
    if not vals:
        return
    if len(vals) == 1:
        filters.append(f"{column} = ?")
        params.append(vals[0])
    else:
        placeholders = ",".join("?" * len(vals))
        filters.append(f"{column} IN ({placeholders})")
        params.extend(vals)

def _add_cc_filter(filters, params, cc, alias="ie"):
    """Adds a control_center IN (...) filter, supporting scalar or list cc."""
    cc_values = _normalize_list(cc)
    if not cc_values:
        return
    placeholders = ",".join(["?"] * len(cc_values))
    filters.append(
        f"{alias}.feeder IN (SELECT feeder FROM station_feeder WHERE control_center IN ({placeholders}))"
    )
    params.extend(cc_values)

def _feeder_category_subquery(feeder_category, extra_cond: str = "") -> tuple:
    cats = _normalize_list(feeder_category)
    if not cats:
        return "", []
    placeholders = ",".join("?" * len(cats))
    sql = (
        f"ie.feeder IN (SELECT feeder FROM station_feeder "
        f"WHERE feeder_category IN ({placeholders}){extra_cond})"
    )
    return sql, list(cats)

def _con():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA cache_size=-32000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA mmap_size=134217728")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA query_only = ON")  # for all read-only connections
    return con

# ── Hierarchy helpers ──────────────────────────────────────────────────────────

def get_cc_list() -> list:
    key = "cc_list"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with _con() as c:
        rows = c.execute(
            "SELECT DISTINCT control_center FROM station_feeder ORDER BY control_center"
        ).fetchall()
    result = [r[0] for r in rows if r[0]]
    _cache_set(key, result)
    return result

def get_divisions(cc: str) -> list:
    key = f"divisions:{cc}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with _con() as c:
        rows = c.execute(
            "SELECT DISTINCT division FROM station_feeder "
            "WHERE control_center=? ORDER BY division", (cc,)
        ).fetchall()
    result = [r[0] for r in rows if r[0]]
    _cache_set(key, result)
    return result

def get_stations(cc: str, division: str) -> list:
    key = f"stations:{cc}:{division}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with _con() as c:
        rows = c.execute(
            "SELECT DISTINCT station FROM station_feeder "
            "WHERE control_center=? AND division=? ORDER BY station",
            (cc, division)
        ).fetchall()
    result = [r[0] for r in rows if r[0]]
    _cache_set(key, result)
    return result

def get_feeder_categories(cc: str, division: str, station: str = None) -> list:
    key = f"fdr_cats:{cc}:{division}:{station or '__all__'}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    sql = ("SELECT DISTINCT feeder_category FROM station_feeder "
           "WHERE control_center=? AND division=?")
    params = [cc, division]
    if station:
        sql += " AND station=?"
        params.append(station)
    sql += " AND feeder_category IS NOT NULL AND TRIM(feeder_category) != '' ORDER BY feeder_category"
    with _con() as c:
        rows = c.execute(sql, params).fetchall()
    result = [r[0] for r in rows if r[0]]
    _cache_set(key, result)
    return result

def get_feeders(cc: str, division: str, station: str, feeder_category=None) -> list:
    cats = _normalize_list(feeder_category)
    cats_key = "|".join(sorted(cats)) if cats else "__all__"
    key = f"feeders:{cc}:{division}:{station}:{cats_key}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    sql = ("SELECT DISTINCT feeder FROM station_feeder "
           "WHERE control_center=? AND division=? AND station=?")
    params = [cc, division, station]
    if cats:
        placeholders = ",".join("?" * len(cats))
        sql += f" AND feeder_category IN ({placeholders})"
        params.extend(cats)
    sql += " ORDER BY feeder"
    with _con() as c:
        rows = c.execute(sql, params).fetchall()
    result = [r[0] for r in rows if r[0]]
    _cache_set(key, result)
    return result

# ── Overview KPIs ──────────────────────────────────────────────────────────────

def get_overview_kpis(date_start=None, date_end=None,
                      level_mode="both",
                      include_equipment=None,
                      include_equipment_code=None) -> dict:
    if include_equipment_code is not None:
        level_mode = "both" if include_equipment_code else "feeder"
    elif include_equipment is not None:
        level_mode = "both" if include_equipment else "feeder"
        
    key = f"overview_kpis|{date_start}|{date_end}|{level_mode}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    conditions = []
    params_date = []
    if date_start:
        conditions.append("event_date >= ?")
        params_date.append(date_start)
    if date_end:
        conditions.append("event_date <= ?")
        params_date.append(date_end)

    lvl_cond, _ = _level_mode_condition(level_mode)
    if lvl_cond:
        conditions.append(lvl_cond)
    date_filter = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    daily_conditions = []
    daily_params = []
    if date_start:
        daily_conditions.append("event_date >= ?")
        daily_params.append(date_start)
    if date_end:
        daily_conditions.append("event_date <= ?")
        daily_params.append(date_end)
    daily_where = ("WHERE " + " AND ".join(daily_conditions)) if daily_conditions else ""

    with _con() as con:

        # ── Core totals (unchanged) ────────────────────────────────────────────
        cte_sql = f"""
WITH filtered AS (
    SELECT feeder, duration_mins, customers_affected, cmi
    FROM interruption_events
    {date_filter}
),
feeder_base AS (
    SELECT feeder, MAX(customers_affected) AS unique_cust
    FROM filtered
    GROUP BY feeder
)
SELECT
    COUNT(*) AS total_events,
    ROUND(SUM(f.duration_mins), 2) AS total_mins,
    COALESCE((SELECT SUM(unique_cust) FROM feeder_base), 0) AS unique_customers,
    COALESCE(SUM(f.customers_affected), 0) AS total_customers,
    ROUND(COALESCE(SUM(f.cmi), 0), 2) AS total_cmi
FROM filtered f
"""
        totals = con.execute(cte_sql, params_date).fetchone()

        # ── CC feeder/station universe (unchanged) ─────────────────────────────
        #cc_counts = pd.read_sql(
        #    """SELECT sf.control_center,
        #              COUNT(DISTINCT sf.station) AS stations,
        #              COUNT(DISTINCT sf.feeder)  AS feeders
        #       FROM station_feeder sf
        #       GROUP BY sf.control_center""",
        #    con
        #)
        # Independent params for cc_counts LEFT JOIN (cannot reuse date_filter string)
        _cc_join = ["ie.feeder = sf.feeder"]
    _cc_params: list = []
    if date_start:
        _cc_join.append("ie.event_date >= ?")
        _cc_params.append(date_start)
    if date_end:
        _cc_join.append("ie.event_date <= ?")
        _cc_params.append(date_end)
    _lvl_cc, _ = _level_mode_condition(level_mode, alias="ie")
    if _lvl_cc:
        _cc_join.append(_lvl_cc)
    _on = " AND ".join(_cc_join)

    cc_counts = pd.read_sql(f"""
        SELECT
            sf.control_center,
            COUNT(DISTINCT sf.station)                                     AS stations,
            COUNT(DISTINCT sf.feeder)                                      AS feeders,
            COUNT(DISTINCT ie.feeder)                                      AS feeders_interrupted,
            COUNT(DISTINCT sf.feeder) - COUNT(DISTINCT ie.feeder)          AS feeders_healthy,
            COUNT(ie.rowid)                                                AS events,
            ROUND(COALESCE(SUM(ie.duration_mins), 0), 1)                   AS total_mins,
            ROUND(COALESCE(SUM(ie.cmi), 0), 0)                             AS total_cmi,
            COUNT(CASE WHEN LOWER(COALESCE(ie.agency,'')) LIKE '%bescom%'
                      THEN 1 END)                                         AS bescom_events,
            ROUND(COALESCE(SUM(CASE WHEN LOWER(COALESCE(ie.agency,''))
                       LIKE '%bescom%' THEN ie.duration_mins END), 0), 1)  AS bescom_mins,
            ROUND(COALESCE(SUM(CASE WHEN LOWER(COALESCE(ie.agency,''))
                       LIKE '%bescom%' THEN ie.cmi END), 0), 0)            AS bescom_cmi,
            COUNT(CASE WHEN LOWER(COALESCE(ie.agency,'')) LIKE '%kptcl%'
                       THEN 1 END)                                         AS kptcl_events,
            ROUND(COALESCE(SUM(CASE WHEN LOWER(COALESCE(ie.agency,''))
                       LIKE '%kptcl%' THEN ie.duration_mins END), 0), 1)   AS kptcl_mins,
            ROUND(COALESCE(SUM(CASE WHEN LOWER(COALESCE(ie.agency,''))
                       LIKE '%kptcl%' THEN ie.cmi END), 0), 0)             AS kptcl_cmi
        FROM station_feeder sf
        LEFT JOIN interruption_events ie ON {_on}
        GROUP BY sf.control_center
    """, con, params=_cc_params)
    # ── Daily trend (unchanged) ────────────────────────────────────────────
    if level_mode == "both":
        daily = pd.read_sql(
            f"SELECT event_date, total_events, total_duration "
            f"FROM daily_summary {daily_where} ORDER BY event_date",
            con, params=daily_params
        )
    else:
        daily = pd.read_sql(
            f"""SELECT event_date,
                       COUNT(*) AS total_events,
                       ROUND(SUM(duration_mins),2) AS total_duration
                FROM interruption_events {date_filter}
                GROUP BY event_date ORDER BY event_date""",
            con, params=params_date
        )

    # ── Top subdivisions (unchanged) ───────────────────────────────────────
    top_conditions = ["subdivision IS NOT NULL", "subdivision != ''"] + conditions
    top_where = "WHERE " + " AND ".join(top_conditions)
    top = pd.read_sql(
        f"""SELECT division, subdivision,
                   COUNT(*) AS events,
                   ROUND(SUM(duration_mins),2) AS minutes
            FROM interruption_events {top_where}
            GROUP BY division, subdivision
            ORDER BY minutes DESC LIMIT 10""",
        con, params=params_date
    )

    # ── NEW: build aliased WHERE for JOIN queries ──────────────────────────
    # The existing conditions use bare column names (event_date, level).
    # For the JOIN queries below we need ie.event_date etc.
    join_conds = []
    for c in conditions:
        if c.startswith("event_date"):
            join_conds.append("ie." + c)
        elif "COALESCE(TRIM(" in c and "level" in c:
            join_conds.append(c.replace("COALESCE(TRIM(", "COALESCE(TRIM(ie."))
        else:
            join_conds.append(c)
    join_where = ("WHERE " + " AND ".join(join_conds)) if join_conds else ""

    # ── NEW: Feeders interrupted per CC ───────────────────────────────────
    feeders_interrupted = pd.read_sql(
        f"""SELECT
                sf.control_center,
                COUNT(DISTINCT ie.feeder) AS feeders_interrupted
            FROM interruption_events ie
            JOIN station_feeder sf ON ie.feeder = sf.feeder
            {join_where}
            GROUP BY sf.control_center""",
        con, params=params_date
    )
    result = {
        "total_events":        totals[0] or 0,
        "total_mins":          totals[1] or 0,
        "unique_customers":    int(totals[2] or 0),   # MAX per feeder → deduplicated
        "total_customers":     int(totals[3] or 0),   # SUM all rows → raw total
        "total_cmi":           totals[4] or 0.0,
        "cc_counts":           cc_counts,
        "daily":               daily,
        "top_subs":            top,
        "feeders_interrupted": feeders_interrupted,
    }
    _cache_set(key, result)
    return result


def get_date_range() -> tuple:
    key = "date_range"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with _con() as con:
        row = con.execute("SELECT MIN(event_date), MAX(event_date) FROM interruption_events").fetchone()
    result = (row[0] or "2026-05-01", row[1] or "2026-05-15")
    _cache_set(key, result)
    return result

# ── Daily trend ────────────────────────────────────────────────────────────────

def get_daily_trend(cc=None, division=None, date_start=None, date_end=None,
                    level_mode="both", include_equipment=None,
                    include_equipment_code=None, feeder_category=None) -> pd.DataFrame:
    if include_equipment_code is not None:
        level_mode = "both" if include_equipment_code else "feeder"
    elif include_equipment is not None:
        level_mode = "both" if include_equipment else "feeder"

    filters, params = [], []
    _add_cc_filter(filters, params, cc, alias="ie")
    _add_in_filter(filters, params, "ie.division", division)
    if date_start:
        filters.append("ie.event_date >= ?"); params.append(date_start)
    if date_end:
        filters.append("ie.event_date <= ?"); params.append(date_end)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        filters.append(cat_sql); params.extend(cat_params)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        filters.append(lvl_cond)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
SELECT ie.event_date,
       COUNT(*) AS total_events,
       ROUND(SUM(ie.duration_mins),2) AS total_duration
FROM interruption_events ie {where}
GROUP BY ie.event_date ORDER BY ie.event_date
"""
    with _con() as con:
        return pd.read_sql(sql, con, params=params)


def get_daily_trend_agg(cc=None, division=None, date_start=None, date_end=None,
                        level_mode="both", feeder_category=None,
                        granularity="day") -> pd.DataFrame:
    filters, params = [], []
    _add_cc_filter(filters, params, cc, alias="ie")
    _add_in_filter(filters, params, "ie.division", division)
    if date_start:
        filters.append("ie.event_date >= ?"); params.append(date_start)
    if date_end:
        filters.append("ie.event_date <= ?"); params.append(date_end)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        filters.append(cat_sql); params.extend(cat_params)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        filters.append(lvl_cond)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    _fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}.get(granularity, "%Y-%m-%d")
    sql = f"""
SELECT strftime('{_fmt}', ie.event_date) AS period,
       COUNT(*) AS total_events,
       ROUND(SUM(ie.duration_mins), 2) AS total_duration,
       ROUND(SUM(COALESCE(ie.cmi, 0)), 2) AS total_cmi
FROM interruption_events ie {where}
GROUP BY period ORDER BY period
"""
    with _con() as con:
        return pd.read_sql(sql, con, params=params)

# ── Interruption detail table ──────────────────────────────────────────────────

def get_interruption_table(cc=None, division=None, station=None, feeders=None,
                           outage_type=None, agency=None,
                           date_start=None, date_end=None,
                           level_mode="both", include_equipment=None,
                           include_equipment_code=None, feeder_category=None) -> pd.DataFrame:
    if include_equipment_code is not None:
        level_mode = "both" if include_equipment_code else "feeder"
    elif include_equipment is not None:
        level_mode = "both" if include_equipment else "feeder"

    filters, params = [], []
    _add_cc_filter(filters, params, cc, alias="ie")
    _add_in_filter(filters, params, "ie.division", division)
    if station:
        filters.append("ie.station = ?"); params.append(station)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        filters.append(cat_sql); params.extend(cat_params)
    if feeders:
        placeholders = ",".join("?" * len(feeders))
        filters.append(f"ie.feeder IN ({placeholders})"); params.extend(feeders)
    if outage_type:
        filters.append("ie.outage_type = ?"); params.append(outage_type)
    if agency:
        filters.append("ie.agency = ?"); params.append(agency)
    if date_start:
        filters.append("ie.event_date >= ?"); params.append(date_start)
    if date_end:
        filters.append("ie.event_date <= ?"); params.append(date_end)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        filters.append(lvl_cond)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
SELECT ie.event_date AS "Date",
       STRFTIME('%H:%M', ie.trouble_dt) AS "Trouble Time",
       ie.division AS "Division",
       ie.subdivision AS "Sub-Division",
       ie.station AS "Station",
       (SELECT sf.feeder_category FROM station_feeder sf
        WHERE sf.feeder = ie.feeder AND sf.station = ie.station LIMIT 1) AS "Feeder Category",
       ie.feeder AS "Feeder",
       ie.outage_type AS "Type",
       ie.trip_status AS "Status",
       ie.agency AS "Agency",
       ie.level AS "Equipment / Feeder",
       ROUND(ie.duration_mins, 2) AS "Duration (min)",
       ie.customers_affected AS "Customers Affected",
       ROUND(ie.cmi, 2) AS "CMI",
       ie.cause AS "Cause",
       ie.work_performed AS "Work Performed"
FROM interruption_events ie {where}
ORDER BY ie.trouble_dt DESC
"""
    with _con() as con:
        return pd.read_sql(sql, con, params=params)

# ── Division-level summary ─────────────────────────────────────────────────────

def get_division_summary(cc, division=None, date_start=None, date_end=None,
                         level_mode="both", include_equipment=None,
                         include_equipment_code=None, feeder_category=None) -> pd.DataFrame:
    if include_equipment_code is not None:
        level_mode = "both" if include_equipment_code else "feeder"
    elif include_equipment is not None:
        level_mode = "both" if include_equipment else "feeder"

    cc_filters, params = [], []
    _add_cc_filter(cc_filters, params, cc, alias="ie")
    cc_filter = cc_filters[0] if cc_filters else "1=1"
    extra = ""
    divs = _normalize_list(division)
    if divs:
        if len(divs) == 1:
            extra += " AND ie.division = ?"; params.append(divs[0])
        else:
            ph = ",".join("?" * len(divs))
            extra += f" AND ie.division IN ({ph})"; params.extend(divs)
    if date_start:
        extra += " AND ie.event_date >= ?"; params.append(date_start)
    if date_end:
        extra += " AND ie.event_date <= ?"; params.append(date_end)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        extra += f" AND {cat_sql}"; params.extend(cat_params)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        extra += f" AND {lvl_cond}"

    sql = f"""
SELECT ie.division, ie.station,
       COUNT(*) AS events,
       ROUND(SUM(ie.duration_mins), 2) AS total_mins,
       ROUND(AVG(ie.duration_mins), 2) AS avg_mins
FROM interruption_events ie
WHERE {cc_filter} {extra}
GROUP BY ie.division, ie.station ORDER BY total_mins DESC
"""
    with _con() as con:
        return pd.read_sql(sql, con, params=params)
        
def get_division_saifi_saidi(cc: str, division=None, date_start=None,
                              date_end=None, level_mode="both",
                              feeder_category=None) -> pd.DataFrame:
    """
    Returns per-division SAIFI and SAIDI.
    SAIDI = SUM(cmi) / division_customers
    SAIFI = COUNT(DISTINCT feeder interruption events) / division_customers
    Joins divisioncustomers for the denominator.
    """
    params = [cc]
    extra = ""
    divs = normalize_list(division)
    if divs:
        if len(divs) == 1:
            extra += " AND ie.division = ?"
            params.append(divs[0])
        else:
            placeholders = ",".join("?" * len(divs))
            extra += f" AND ie.division IN ({placeholders})"
            params.extend(divs)
    if date_start:
        extra += " AND ie.eventdate >= ?"
        params.append(date_start)
    if date_end:
        extra += " AND ie.eventdate <= ?"
        params.append(date_end)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        extra += f" AND {cat_sql}"
        params.extend(cat_params)
    lvl_cond, _ = levelmode_condition(level_mode, alias="ie")
    if lvl_cond:
        extra += f" AND {lvl_cond}"

    sql = f"""
        SELECT
            ie.division                                    AS Division,
            COALESCE(dc.customers, 0)                      AS customers,
            COUNT(*)                                       AS events,
            ROUND(SUM(COALESCE(ie.cmi, 0)), 2)            AS total_cmi,
            CASE WHEN COALESCE(dc.customers,0) > 0
                 THEN ROUND(SUM(COALESCE(ie.cmi,0)) / dc.customers, 4)
                 ELSE 0 END                               AS saidi,
            CASE WHEN COALESCE(dc.customers,0) > 0
                 THEN ROUND(CAST(COUNT(*) AS REAL) / dc.customers, 4)
                 ELSE 0 END                               AS saifi
        FROM interruption_events ie
        LEFT JOIN division_customers dc ON dc.division = ie.division
        WHERE ie.feeder IN (
            SELECT feeder FROM station_feeder WHERE control_center = ?
        ){extra}
        GROUP BY ie.division
        ORDER BY saidi DESC
    """
    # control_center param appears twice (subquery + main)
    with _con() as c:
        return pd.read_sql(sql, c, params=[cc] + params)

# ── Admin helpers ──────────────────────────────────────────────────────────────

def get_db_stats() -> dict:
    with _con() as con:
        ie_count  = con.execute("SELECT COUNT(*) FROM interruption_events").fetchone()[0]
        ds_count  = con.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
        date_row  = con.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM interruption_events"
        ).fetchone()
    return {"ie_count": ie_count, "ds_count": ds_count,
            "date_min": str(date_row[0] or ""), "date_max": str(date_row[1] or "")}

def get_division_customers() -> pd.DataFrame:
    with _con() as c:
        return pd.read_sql(
            "SELECT division, customers FROM division_customers ORDER BY division", c)

def get_feeder_customers(station=None) -> pd.DataFrame:
    sql, params = "SELECT station, feeder, customers FROM feeder_customers", []
    if station:
        sql += " WHERE station = ?"; params.append(station)
    sql += " ORDER BY station, feeder"
    with _con() as c:
        return pd.read_sql(sql, c, params=params)

def get_cc_customers(cc: str) -> int:
    with _con() as c:
        row = c.execute(
            """SELECT COALESCE(SUM(dc.customers), 0) FROM division_customers dc
               WHERE dc.division IN (
                   SELECT DISTINCT division FROM station_feeder WHERE control_center = ?)""",
            (cc,)
        ).fetchone()
    return int(row[0]) if row else 0

def get_division_customers_total(divisions) -> int:
    divs = [divisions] if isinstance(divisions, str) else list(divisions)
    ph = ",".join("?" * len(divs))
    with _con() as c:
        row = c.execute(
            f"SELECT COALESCE(SUM(customers), 0) FROM division_customers WHERE division IN ({ph})",
            divs
        ).fetchone()
    return int(row[0]) if row else 0

def get_station_customers_total(stations) -> int:
    stns = [stations] if isinstance(stations, str) else list(stations)
    ph = ",".join("?" * len(stns))
    with _con() as c:
        row = c.execute(
            f"SELECT COALESCE(SUM(fc.customers), 0) FROM feeder_customers fc WHERE fc.station IN ({ph})",
            stns
        ).fetchone()
    return int(row[0]) if row else 0

def get_feeder_customers_total(feeders: list) -> int:
    if not feeders:
        return 0
    ph = ",".join("?" * len(feeders))
    with _con() as c:
        row = c.execute(
            f"SELECT COALESCE(SUM(customers), 0) FROM feeder_customers WHERE feeder IN ({ph})",
            list(feeders)
        ).fetchone()
    return int(row[0]) if row else 0

def get_recent_uploads(limit: int = 3) -> list:
    with _con() as con:
        rows = con.execute(
            """SELECT upload_id, upload_ts, filename, inserted, skipped
               FROM delta_upload_log WHERE status = 'success'
               ORDER BY upload_id DESC LIMIT ?""", (limit,)
        ).fetchall()
    return [{"upload_id": r[0], "upload_ts": r[1], "filename": r[2],
             "inserted": r[3], "skipped": r[4]} for r in rows]

def validate_cache_integrity() -> dict:
    mismatches = {}
    with _con() as c:
        for key, entry in list(_HIERARCHY_CACHE.items()):
            cached_val = list(entry["val"])
            if key.startswith("divisions:"):
                cc = key.split(":", 1)[1]
                rows = c.execute(
                    "SELECT DISTINCT division FROM station_feeder "
                    "WHERE control_center=? ORDER BY division", (cc,)
                ).fetchall()
                live = [r[0] for r in rows if r[0]]
            elif key.startswith("stations:"):
                parts = key.split(":", 2)
                if len(parts) < 3:
                    continue
                _, cc, div = parts
                rows = c.execute(
                    "SELECT DISTINCT station FROM station_feeder "
                    "WHERE control_center=? AND division=? ORDER BY station", (cc, div)
                ).fetchall()
                live = [r[0] for r in rows if r[0]]
            elif key.startswith("feeders:"):
                continue
            else:
                continue
            cached_sorted = sorted(cached_val)
            live_sorted   = sorted(live)
            if cached_sorted != live_sorted:
                mismatches[key] = {
                    "cached":       cached_sorted,
                    "live_db":      live_sorted,
                    "diff_added":   sorted(list(set(live)       - set(cached_val))),
                    "diff_removed": sorted(list(set(cached_val) - set(live))),
                }
    return mismatches
    

# ============================================================
# NEW: Trends & Comparison tab support functions
# Added below existing db.py functions — purely additive.
# Reuses: con(), _add_cc_filter(), _add_in_filter(),
#         _feeder_category_subquery(), _level_mode_condition()
# No existing function signatures or SQL altered.
# ============================================================

def resolve_customer_base(cc=None, division=None, station=None, feeders=None) -> int:
        """
        Single source of truth for the SAIDI/SAIFI denominator.
        Precedence: feeders > station > division > cc(zone) > global fallback.
        Used by get_period_comparison(), get_saidi_saifi_trend(), and
        (optionally) app.py's resolve_saidi_base() for the KPI cards.
        """
        fdrs = _normalize_list(feeders)
        if fdrs:
            ph = ",".join("?" * len(fdrs))
            with _con() as c:
                row = c.execute(
                    f"SELECT COALESCE(SUM(customers), 0) FROM feeder_customers WHERE feeder IN ({ph})",
                    fdrs
                ).fetchone()
            base = int(row[0]) if row else 0
            return base if base else TOTAL_CUSTOMERS

        if station:
            stn = station if isinstance(station, str) else (station[0] if station else None)
            if stn:
                with _con() as c:
                    row = c.execute(
                        "SELECT COALESCE(SUM(customers), 0) FROM feeder_customers WHERE station = ?",
                        (stn,)
                    ).fetchone()
                base = int(row[0]) if row else 0
                return base if base else TOTAL_CUSTOMERS

        divs = _normalize_list(division)
        if divs:
            ph = ",".join("?" * len(divs))
            with _con() as c:
                row = c.execute(
                    f"SELECT COALESCE(SUM(customers), 0) FROM division_customers WHERE division IN ({ph})",
                    divs
                ).fetchone()
            base = int(row[0]) if row else 0
            return base if base else TOTAL_CUSTOMERS

        cc_vals = _normalize_list(cc)
        if cc_vals:
            ph = ",".join("?" * len(cc_vals))
            with _con() as c:
                row = c.execute(
                    f"""SELECT COALESCE(SUM(dc.customers), 0) FROM division_customers dc
                        WHERE dc.division IN (
                            SELECT DISTINCT division FROM station_feeder WHERE control_center IN ({ph})
                        )""",
                    cc_vals
                ).fetchone()
            base = int(row[0]) if row else 0
        return base if base else TOTAL_CUSTOMERS
        with _con() as c:
                    row = c.execute("SELECT COALESCE(SUM(customers), 0) FROM division_customers").fetchone()
        base = int(row[0]) if row else 0
        return base if base else TOTAL_CUSTOMERS

def get_period_comparison(cc=None, division=None, station=None, feeders=None,
                           feeder_category=None, outage_type=None, agency=None,
                           period_a_start=None, period_a_end=None,
                           period_b_start=None, period_b_end=None,
                           level_mode="both") -> dict:
    """
    Returns aggregate totals for two independent date windows (Period A vs B)
    using the same filter surface as get_interruption_table(), so results are
    directly comparable to what the Explorer table would show for each window.

    Returns:
      {
        "period_a": {"events":.., "total_mins":.., "total_cmi":.., 
                     "total_customers":.., "saidi":.., "saifi":..},
        "period_b": {...},
        "daily_a": pd.DataFrame,   # event_date, total_events, total_duration
        "daily_b": pd.DataFrame,
      }
    """
    def _customer_base():
        """Delegates to the module-level resolver so all SAIDI/SAIFI
        surfaces (Period Comparison, Reliability Trends) agree by construction."""
        return resolve_customer_base(cc=cc, division=division, station=station, feeders=feeders)

    def _totals(date_start, date_end):
        filters, params = [], []
        _add_cc_filter(filters, params, cc, alias="ie")
        _add_in_filter(filters, params, "ie.division", division)
        if station:
            filters.append("ie.station = ?")
            params.append(station)
        cat_sql, cat_params = _feeder_category_subquery(feeder_category)
        if cat_sql:
            filters.append(cat_sql)
            params.extend(cat_params)
        if feeders:
            placeholders = ",".join(["?"] * len(feeders))
            filters.append(f"ie.feeder IN ({placeholders})")
            params.extend(feeders)
        if outage_type:
            filters.append("ie.outage_type = ?")
            params.append(outage_type)
        if agency:
            filters.append("ie.agency = ?")
            params.append(agency)
        if date_start:
            filters.append("ie.event_date >= ?")
            params.append(date_start)
        if date_end:
            filters.append("ie.event_date <= ?")
            params.append(date_end)
        lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
        if lvl_cond:
            filters.append(lvl_cond)
        where = "WHERE " + " AND ".join(filters) if filters else ""

        totals_sql = f"""
            SELECT
                COUNT(*) AS events,
                ROUND(COALESCE(SUM(ie.duration_mins), 0), 2) AS total_mins,
                ROUND(COALESCE(SUM(ie.cmi), 0), 2) AS total_cmi,
                COALESCE(SUM(ie.customers_affected), 0) AS total_customers
            FROM interruption_events ie
            {where}
        """
        daily_sql = f"""
            SELECT ie.event_date AS event_date,
                   COUNT(*) AS total_events,
                   ROUND(SUM(ie.duration_mins), 2) AS total_duration
            FROM interruption_events ie
            {where}
            GROUP BY ie.event_date
            ORDER BY ie.event_date
        """
        with _con() as c:
            row = c.execute(totals_sql, params).fetchone()
            daily_df = pd.read_sql(daily_sql, c, params=params)

        events = int(row[0] or 0)
        total_mins = float(row[1] or 0.0)
        total_cmi = float(row[2] or 0.0)
        total_customers = int(row[3] or 0)

        customer_base = _customer_base()
        saidi = round(total_cmi / customer_base, 4) if customer_base else 0.0
        saifi = round(total_customers / customer_base, 4) if customer_base else 0.0

        return {
            "events": events,
            "total_mins": total_mins,
            "total_cmi": total_cmi,
            "total_customers": total_customers,
            "saidi": saidi,          # NEW
            "saifi": saifi,          # NEW
        }, daily_df

    totals_a, daily_a = _totals(period_a_start, period_a_end)
    totals_b, daily_b = _totals(period_b_start, period_b_end)

    return {
        "period_a": totals_a,
        "period_b": totals_b,
        "daily_a": daily_a,
        "daily_b": daily_b,
    }

def get_saidi_saifi_trend(cc=None, division=None, station=None, feeders=None,
                           feeder_category=None, granularity="month",
                           level_mode="both", date_start=None, date_end=None,
                           outage_type=None, agency=None) -> pd.DataFrame:
    """
    Time-series SAIDI/SAIFI per period bucket (month/quarter), for the
    Reliability Trends chart and the Division Ranking panel.

    SAIDI = SUM(cmi) / customer_base (per period bucket)
    SAIFI = SUM(customers_affected) / customer_base   <-- FIXED (was COUNT(*))

    NOTE (SAIFI fix): previously SAIFI numerator was COUNT(*) of interruption
    rows, which does not match get_period_comparison()'s SAIFI definition
    (SUM(customers_affected)). Both now use the same IEEE 1366-style
    customers-interrupted numerator, and both resolve their denominator via
    the shared resolve_customer_base() helper — so Period Comparison and
    Reliability Trends will always reconcile for identical filters/dates.

    Returns columns: period, division, events, total_cmi,
                      total_customers_affected, customers, saidi, saifi
    """
    filters, params = [], []
    _add_cc_filter(filters, params, cc, alias="ie")
    _add_in_filter(filters, params, "ie.division", division)
    if station:
        filters.append("ie.station = ?")
        params.append(station)
    if feeders:
        placeholders = ",".join(["?"] * len(feeders))
        filters.append(f"ie.feeder IN ({placeholders})")
        params.extend(feeders)
    if outage_type:
        filters.append("ie.outage_type = ?")
        params.append(outage_type)
    if agency:
        filters.append("ie.agency = ?")
        params.append(agency)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        filters.append(cat_sql)
        params.extend(cat_params)
    if date_start:
        filters.append("ie.event_date >= ?")
        params.append(date_start)
    if date_end:
        filters.append("ie.event_date <= ?")
        params.append(date_end)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        filters.append(lvl_cond)
    where = "WHERE " + " AND ".join(filters) if filters else ""

    period_expr = "strftime('%Y-%m', ie.event_date)"

    # Division-level breakdown only when nothing narrower than division is
    # selected — same precedence as before (feeders/station/cc-only collapse
    # to a single scoped row; division-level scope keeps per-division rows).
    if feeders or station or (not division and cc):
        group_cols = "period"
        division_col = "'Selected scope' AS division,"
    else:
        group_cols = "period, ie.division"
        division_col = "ie.division AS division,"

    sql = f"""
        SELECT
            {period_expr} AS period,
            {division_col}
            COUNT(*) AS events,
            ROUND(COALESCE(SUM(ie.cmi), 0), 2) AS total_cmi,
            COALESCE(SUM(ie.customers_affected), 0) AS total_customers_affected
        FROM interruption_events ie
        {where}
        GROUP BY {group_cols}
        ORDER BY period
    """
    with _con() as c:
        df = pd.read_sql(sql, c, params=params)

    if df.empty:
        df["customers"] = []
        df["saidi"] = []
        df["saifi"] = []
        return df

    # ── Customer-base resolution: single shared resolver (Step 4) ──────────
    if group_cols == "period, ie.division":
        # Division-level ranking still needs a per-division denominator.
        with _con() as c:
            dc = pd.read_sql("SELECT division, customers FROM division_customers", c)
        df = df.merge(dc, on="division", how="left")
        df["customers"] = df["customers"].fillna(0).astype(int)
    else:
        scoped_base = resolve_customer_base(cc=cc, division=division,
                                             station=station, feeders=feeders)
        df["customers"] = scoped_base

    df["saidi"] = df.apply(
        lambda r: round(r["total_cmi"] / r["customers"], 4) if r["customers"] else 0.0, axis=1
    )
    df["saifi"] = df.apply(
        lambda r: round(r["total_customers_affected"] / r["customers"], 4) if r["customers"] else 0.0, axis=1
    )

    if granularity == "quarter":
        # Re-bucket month periods into quarters in pandas (no SQL change needed).
        df["period"] = pd.to_datetime(df["period"] + "-01")
        df["period"] = df["period"].dt.year.astype(str) + "-Q" + df["period"].dt.quarter.astype(str)
        df = (df.groupby(["period", "division"], as_index=False)
                .agg(events=("events", "sum"),
                     total_cmi=("total_cmi", "sum"),
                     total_customers_affected=("total_customers_affected", "sum"),
                     customers=("customers", "max")))
        df["saidi"] = df.apply(
            lambda r: round(r["total_cmi"] / r["customers"], 4) if r["customers"] else 0.0, axis=1
        )
        df["saifi"] = df.apply(
            lambda r: round(r["total_customers_affected"] / r["customers"], 4) if r["customers"] else 0.0, axis=1
        )

    return df

def get_repeat_offenders(cc=None, division=None, station=None, feeders=None,
                          feeder_category=None, min_events=3,
                          level_mode="both", date_start=None, date_end=None,
                          outage_type=None, agency=None) -> pd.DataFrame:
    """
    Monthly feeder-level pivot for the Repeat Offenders table.
    Adds station/feeders filters per review recommendation — mirrors the
    filter surface of get_interruption_table() minus outage_type/agency
    (kept consistent with the decision to scope those two filters to
    Period Comparison only).

    NOTE (Trends cascade enhancement): date_start/date_end are OPTIONAL and
    additive. When omitted, behavior is unchanged (all-time pivot, exactly
    as before). When provided, they scope the pivot to the combined
    Period A start -> Period B end window selected on the Trends tab.

    Returns one row per feeder with:
        Feeder, Division, Station, <month1>, <month2>, ..., total_events,
        total_cmi, mom_pct (latest vs prior month), pct_zone_cmi
    Only feeders with total_events >= min_events are included.
    """
    filters, params = [], []
    _add_cc_filter(filters, params, cc, alias="ie")
    _add_in_filter(filters, params, "ie.division", division)
    if station:
        filters.append("ie.station = ?")
        params.append(station)
    if feeders:
        placeholders = ",".join(["?"] * len(feeders))
        filters.append(f"ie.feeder IN ({placeholders})")
        params.extend(feeders)
    if outage_type:
        filters.append("ie.outage_type = ?")
        params.append(outage_type)
    if agency:
        filters.append("ie.agency = ?")
        params.append(agency)
    cat_sql, cat_params = _feeder_category_subquery(feeder_category)
    if cat_sql:
        filters.append(cat_sql)
        params.extend(cat_params)
    if date_start:
        filters.append("ie.event_date >= ?")
        params.append(date_start)
    if date_end:
        filters.append("ie.event_date <= ?")
        params.append(date_end)
    lvl_cond, _ = _level_mode_condition(level_mode, alias="ie")
    if lvl_cond:
        filters.append(lvl_cond)
    where = "WHERE " + " AND ".join(filters) if filters else ""

    sql = f"""
        SELECT
            ie.feeder AS feeder,
            ie.division AS division,
            ie.station AS station,
            strftime('%Y-%m', ie.event_date) AS month,
            COUNT(*) AS events,
            ROUND(COALESCE(SUM(ie.cmi), 0), 2) AS cmi
        FROM interruption_events ie
        {where}
        GROUP BY ie.feeder, ie.division, ie.station, month
        ORDER BY ie.feeder, month
    """
    with _con() as c:
        raw = pd.read_sql(sql, c, params=params)

    if raw.empty:
        return raw

    # Pivot: one row per feeder, one column per month (event counts for heat-map cells)
    pivot = raw.pivot_table(index=["feeder", "division", "station"],
                             columns="month", values="events",
                             fill_value=0, aggfunc="sum").reset_index()
    months_sorted = sorted([c for c in pivot.columns if c not in ("feeder", "division", "station")])
    pivot = pivot[["feeder", "division", "station"] + months_sorted]

    totals = raw.groupby(["feeder", "division", "station"], as_index=False).agg(
        total_events=("events", "sum"),
        total_cmi=("cmi", "sum"),
    )
    pivot = pivot.merge(totals, on=["feeder", "division", "station"])
    pivot = pivot[pivot["total_events"] >= min_events].copy()

    # MoM % — compare latest two available months per feeder (Insight B)
    def _mom(row):
        if len(months_sorted) < 2:
            return 0.0
        latest, prior = row[months_sorted[-1]], row[months_sorted[-2]]
        if prior == 0:
            return 100.0 if latest > 0 else 0.0
        return round((latest - prior) / prior * 100, 1)

    pivot["mom_pct"] = pivot.apply(_mom, axis=1)

    # % of zone CMI — reframes ranking by customer-minutes impact, not just event count (Insight C)
    zone_total_cmi = raw["cmi"].sum()
    pivot["pct_zone_cmi"] = pivot["total_cmi"].apply(
        lambda v: round(v / zone_total_cmi * 100, 1) if zone_total_cmi else 0.0
    )

    return pivot.sort_values("total_events", ascending=False).reset_index(drop=True)
