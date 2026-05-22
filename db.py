"""SQLite 데이터베이스 레이어"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.db_path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init(self):
        with self.conn() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS photos (
                    id       TEXT PRIMARY KEY,
                    filename TEXT,
                    filepath TEXT,
                    uploaded_at TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS records (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code     TEXT    DEFAULT '',
                    site_name     TEXT    DEFAULT '',
                    company       TEXT    DEFAULT '',
                    location      TEXT    DEFAULT '',
                    measurer      TEXT    DEFAULT '',
                    measure_date  TEXT    NOT NULL,
                    slot          TEXT    NOT NULL,
                    week_monday   TEXT    DEFAULT '',
                    measure_time  TEXT    DEFAULT '',
                    temperature   REAL,
                    humidity      REAL,
                    feels_like    REAL,
                    heat_level    TEXT    DEFAULT '',
                    action        TEXT    DEFAULT 'N/A',
                    other_content TEXT    DEFAULT '',
                    notes         TEXT    DEFAULT '',
                    photo_id      TEXT    DEFAULT '',
                    created_at    TEXT    DEFAULT (datetime('now','localtime')),
                    updated_at    TEXT    DEFAULT (datetime('now','localtime')),
                    UNIQUE(site_code, location, measure_date, slot)
                );
            """)

    # ── Photos ────────────────────────────────────────────────────────────────
    def save_photo(self, photo_id: str, filename: str, filepath: str):
        with self.conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO photos (id,filename,filepath) VALUES (?,?,?)",
                (photo_id, filename, filepath),
            )

    def get_photo(self, photo_id: str) -> Optional[dict]:
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_photo(self, photo_id: str):
        with self.conn() as con:
            con.execute("DELETE FROM photos WHERE id=?", (photo_id,))
            con.execute("UPDATE records SET photo_id='' WHERE photo_id=?", (photo_id,))

    # ── Records ───────────────────────────────────────────────────────────────
    def get_records(self, site_code: str = "", week_monday: str = "", location: str = "", company: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT * FROM records WHERE 1=1", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            if location:
                q += " AND location=?"; p.append(location)
            if company:
                q += " AND company=?"; p.append(company)
            q += " ORDER BY measure_date, slot"
            return [dict(r) for r in con.execute(q, p).fetchall()]

    def get_companies(self, site_code: str = "", week_monday: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT company FROM records WHERE company!=''", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            q += " ORDER BY company"
            return [r[0] for r in con.execute(q, p).fetchall()]

    def get_locations(self, site_code: str = "", company: str = "", week_monday: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT location FROM records WHERE location!=''", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if company:
                q += " AND company=?"; p.append(company)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            q += " ORDER BY location"
            return [r[0] for r in con.execute(q, p).fetchall()]

    def upsert_record(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("""
                INSERT INTO records (
                    site_code,site_name,company,location,measurer,
                    measure_date,slot,week_monday,measure_time,
                    temperature,humidity,feels_like,heat_level,
                    action,other_content,notes,photo_id,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(site_code,location,measure_date,slot) DO UPDATE SET
                    site_name=excluded.site_name, company=excluded.company,
                    measurer=excluded.measurer, week_monday=excluded.week_monday,
                    measure_time=excluded.measure_time,
                    temperature=excluded.temperature, humidity=excluded.humidity,
                    feels_like=excluded.feels_like, heat_level=excluded.heat_level,
                    action=excluded.action, other_content=excluded.other_content,
                    notes=excluded.notes, photo_id=excluded.photo_id,
                    updated_at=excluded.updated_at
            """, (
                data.get("site_code",""), data.get("site_name",""),
                data.get("company",""),   data.get("location",""),
                data.get("measurer",""),  data["measure_date"],
                data["slot"],             data.get("week_monday",""),
                data.get("measure_time",""),
                data.get("temperature"),  data.get("humidity"),
                data.get("feels_like"),   data.get("heat_level",""),
                data.get("action","N/A"), data.get("other_content",""),
                data.get("notes",""),     data.get("photo_id",""), now,
            ))
            row = con.execute(
                "SELECT id FROM records WHERE site_code=? AND location=? AND measure_date=? AND slot=?",
                (data.get("site_code",""), data.get("location",""),
                 data["measure_date"], data["slot"]),
            ).fetchone()
            return row[0] if row else -1

    def update_record(self, rec_id: int, data: dict):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("""
                UPDATE records SET
                    site_code=?,site_name=?,company=?,location=?,measurer=?,
                    measure_time=?,temperature=?,humidity=?,feels_like=?,
                    heat_level=?,action=?,other_content=?,notes=?,updated_at=?
                WHERE id=?
            """, (
                data.get("site_code",""), data.get("site_name",""),
                data.get("company",""),   data.get("location",""),
                data.get("measurer",""),  data.get("measure_time",""),
                data.get("temperature"),  data.get("humidity"),
                data.get("feels_like"),   data.get("heat_level",""),
                data.get("action","N/A"), data.get("other_content",""),
                data.get("notes",""), now, rec_id,
            ))

    def get_record_photo_id(self, rec_id: int) -> str:
        with self.conn() as con:
            row = con.execute("SELECT photo_id FROM records WHERE id=?", (rec_id,)).fetchone()
            return row[0] if row else ""

    def delete_record(self, rec_id: int):
        with self.conn() as con:
            con.execute("DELETE FROM records WHERE id=?", (rec_id,))

    # ── Autocomplete / History ─────────────────────────────────────────────────
    def get_autocomplete(self, site_code: str = "") -> dict:
        with self.conn() as con:
            codes = con.execute(
                "SELECT DISTINCT site_code,site_name FROM records "
                "WHERE site_code!='' ORDER BY site_code"
            ).fetchall()
            if site_code:
                companies = con.execute(
                    "SELECT DISTINCT company FROM records "
                    "WHERE company!='' AND UPPER(site_code)=UPPER(?) ORDER BY company",
                    (site_code,)
                ).fetchall()
                locations = con.execute(
                    "SELECT DISTINCT location FROM records "
                    "WHERE location!='' AND UPPER(site_code)=UPPER(?) ORDER BY location",
                    (site_code,)
                ).fetchall()
            else:
                companies = con.execute(
                    "SELECT DISTINCT company FROM records WHERE company!='' ORDER BY company"
                ).fetchall()
                locations = con.execute(
                    "SELECT DISTINCT location FROM records WHERE location!='' ORDER BY location"
                ).fetchall()
        return {
            "site_codes": [{"code": r[0], "name": r[1]} for r in codes],
            "companies":  [r[0] for r in companies],
            "locations":  [r[0] for r in locations],
        }

    def get_available_weeks(self, site_code: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT week_monday FROM records WHERE week_monday!=''", []
            if site_code:
                q += " AND site_code=?"; p.append(site_code)
            q += " ORDER BY week_monday DESC"
            return [r[0] for r in con.execute(q, p).fetchall()]
