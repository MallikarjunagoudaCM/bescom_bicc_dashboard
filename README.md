# BESCOM BICC General Managers Dashboard

Plotly Dash 4-section (Landing / Overview / Interruption Explorer / Admin) utility
outage monitoring dashboard for BESCOM BICC zones.

## Run
```
pip install -r requirements.txt
python app.py
```
App serves on the HOST/PORT configured in `config.py`.

## Structure
- `app_working.py` — Dash app, layouts, callbacks
- `db.py` — SQLite data access layer (WAL mode, hierarchy cache)
- `delta_load.py` — incremental XLSX upload + rollback logic
- `db_setup.py` / `db_rollback_migration.py` — schema setup & migration helpers
- `config.py` — thresholds, paths, admin credentials (use env vars in production)
- `assets/` — CSS (numeric-prefix load order) + BESCOM logo + keyboard shortcut JS
- `bicc.db` — SQLite database (gitignored — not committed)

## Assets load order
`01_variables.css` -> `02_base.css` -> `03_components.css` -> `04_table.css` ->
`05_admin.css` -> `06_landing.css` -> `07_shortcut_toggle.css` (Dash auto-loads
alphabetically; the `07_` prefix ensures this overrides `03_components.css`).

## Keyboard shortcuts
- **Ctrl+M** (or Cmd+M) — toggles visibility of the Overview tab's
  Feeder/Equipment/Both level-scope selector (hidden by default).

## Admin
Default login is read from `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `config.py`.
**Move these to environment variables before deploying publicly.**
