"""취약근로자 관리 데이터베이스 레이어"""

import hashlib, hmac, json, os, sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional


class WorkersDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _add_worker_columns_if_missing(self, con):
        cols = {row[1] for row in con.execute("PRAGMA table_info(vw_workers)")}
        new_cols = [
            ("worker_code",       "TEXT DEFAULT ''"),
            ("education_date",    "TEXT DEFAULT ''"),
            ("name_korean",       "TEXT DEFAULT ''"),
            ("job_type",          "TEXT DEFAULT ''"),
            ("nationality",       "TEXT DEFAULT ''"),
            ("birth_date",        "TEXT DEFAULT ''"),
            ("phone",             "TEXT DEFAULT ''"),
            ("residence_status",  "TEXT DEFAULT ''"),
            ("residence_expiry",  "TEXT DEFAULT ''"),
            ("gender",            "TEXT DEFAULT ''"),
            ("last_exam_date",    "TEXT DEFAULT ''"),
        ]
        for col, defn in new_cols:
            if col not in cols:
                con.execute(f"ALTER TABLE vw_workers ADD COLUMN {col} {defn}")

    def _init(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL")
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS vw_workers (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code           TEXT    NOT NULL,
                    worker_code         TEXT    DEFAULT '',
                    education_date      TEXT    DEFAULT '',
                    name                TEXT    NOT NULL,
                    name_korean         TEXT    DEFAULT '',
                    company             TEXT    NOT NULL,
                    job_type            TEXT    DEFAULT '',
                    nationality         TEXT    DEFAULT '',
                    birth_date          TEXT    DEFAULT '',
                    birth_year          INTEGER,
                    phone               TEXT    DEFAULT '',
                    residence_status    TEXT    DEFAULT '',
                    residence_expiry    TEXT    DEFAULT '',
                    gender              TEXT    DEFAULT '',
                    last_exam_date      TEXT    DEFAULT '',
                    vulnerability_types TEXT    DEFAULT '[]',
                    diseases            TEXT    DEFAULT '',
                    work_restrictions   TEXT    DEFAULT '',
                    is_vulnerable       INTEGER DEFAULT 0,
                    notes               TEXT    DEFAULT '',
                    created_at          TEXT    DEFAULT (datetime('now','localtime')),
                    updated_at          TEXT    DEFAULT (datetime('now','localtime')),
                    deleted_at          TEXT
                );

                CREATE TABLE IF NOT EXISTS vw_health_exams (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id   INTEGER NOT NULL,
                    site_code   TEXT    NOT NULL,
                    exam_type   TEXT    NOT NULL,
                    exam_date   TEXT    NOT NULL,
                    key_values  TEXT    DEFAULT '{}',
                    photo_id    TEXT    DEFAULT '',
                    notes       TEXT    DEFAULT '',
                    created_at  TEXT    DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS vw_site_locations (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code     TEXT    NOT NULL,
                    location_name TEXT    NOT NULL,
                    created_at    TEXT    DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS vw_company_pins (
                    site_code   TEXT NOT NULL,
                    company     TEXT NOT NULL,
                    pin_hash    TEXT NOT NULL,
                    updated_at  TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (site_code, company)
                );

                CREATE TABLE IF NOT EXISTS vw_daily_attendance (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code     TEXT NOT NULL,
                    company       TEXT NOT NULL,
                    work_date     TEXT NOT NULL,
                    worker_id     INTEGER NOT NULL,
                    work_location TEXT DEFAULT '',
                    created_at    TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(work_date, company, worker_id)
                );

                CREATE TABLE IF NOT EXISTS vw_health_records (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code     TEXT NOT NULL,
                    company       TEXT NOT NULL,
                    record_date   TEXT NOT NULL,
                    slot          TEXT NOT NULL,
                    heat_level    TEXT DEFAULT '',
                    feels_like    REAL,
                    worker_id     INTEGER NOT NULL,
                    body_temp     REAL,
                    measure_time  TEXT DEFAULT '',
                    health_status TEXT DEFAULT '양호',
                    notes         TEXT DEFAULT '',
                    created_at    TEXT DEFAULT (datetime('now','localtime')),
                    updated_at    TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(record_date, slot, company, worker_id)
                );

                CREATE TABLE IF NOT EXISTS vw_health_photos (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_code   TEXT NOT NULL,
                    company     TEXT NOT NULL,
                    record_date TEXT NOT NULL,
                    slot        TEXT NOT NULL,
                    photo_type  TEXT NOT NULL,
                    photo_id    TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE INDEX IF NOT EXISTS idx_vw_workers_site
                    ON vw_workers(site_code, company);
                CREATE INDEX IF NOT EXISTS idx_vw_attendance_date
                    ON vw_daily_attendance(site_code, work_date, company);
                CREATE INDEX IF NOT EXISTS idx_vw_health_date
                    ON vw_health_records(site_code, record_date, slot, company);
            """)
            self._add_worker_columns_if_missing(con)
            con.commit()
        finally:
            con.close()

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _hash(pin: str) -> str:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pin.strip().encode(), salt, 260_000)
        return salt.hex() + ":" + dk.hex()

    @staticmethod
    def _verify(pin: str, stored: str) -> bool:
        pin_bytes = pin.strip().encode()
        if ":" in stored:
            try:
                salt_hex, dk_hex = stored.split(":", 1)
                salt = bytes.fromhex(salt_hex)
                dk = hashlib.pbkdf2_hmac("sha256", pin_bytes, salt, 260_000)
                return hmac.compare_digest(dk.hex(), dk_hex)
            except Exception:
                return False
        else:
            return hmac.compare_digest(hashlib.sha256(pin_bytes).hexdigest(), stored)

    @staticmethod
    def _parse_vtypes(row: dict) -> dict:
        try:
            row['vulnerability_types'] = json.loads(row.get('vulnerability_types') or '[]')
        except Exception:
            row['vulnerability_types'] = []
        return row

    # ── Workers ───────────────────────────────────────────────────────────────
    def get_workers(self, site_code: str, company: str = "") -> list:
        with self.conn() as con:
            q = "SELECT * FROM vw_workers WHERE UPPER(site_code)=UPPER(?) AND deleted_at IS NULL"
            p = [site_code]
            if company:
                q += " AND company=?"; p.append(company)
            q += " ORDER BY company, is_vulnerable DESC, name"
            return [self._parse_vtypes(dict(r)) for r in con.execute(q, p).fetchall()]

    def insert_worker(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        vtypes = json.dumps(data.get('vulnerability_types', []), ensure_ascii=False)
        is_vul = 1 if data.get('vulnerability_types') else 0
        with self.conn() as con:
            cur = con.execute("""
                INSERT INTO vw_workers
                    (site_code,worker_code,education_date,name,name_korean,company,
                     job_type,nationality,birth_date,birth_year,phone,
                     residence_status,residence_expiry,gender,last_exam_date,
                     vulnerability_types,diseases,work_restrictions,
                     is_vulnerable,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (data['site_code'], data.get('worker_code',''), data.get('education_date',''),
                  data['name'], data.get('name_korean',''), data['company'],
                  data.get('job_type',''), data.get('nationality',''),
                  data.get('birth_date',''), data.get('birth_year'),
                  data.get('phone',''), data.get('residence_status',''), data.get('residence_expiry',''),
                  data.get('gender',''), data.get('last_exam_date',''),
                  vtypes, data.get('diseases',''), data.get('work_restrictions',''),
                  is_vul, data.get('notes',''), now, now))
            return cur.lastrowid

    def update_worker(self, worker_id: int, data: dict):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        vtypes = json.dumps(data.get('vulnerability_types', []), ensure_ascii=False)
        is_vul = 1 if data.get('vulnerability_types') else 0
        with self.conn() as con:
            con.execute("""
                UPDATE vw_workers SET
                    worker_code=?,education_date=?,name=?,name_korean=?,company=?,
                    job_type=?,nationality=?,birth_date=?,birth_year=?,phone=?,
                    residence_status=?,residence_expiry=?,gender=?,last_exam_date=?,
                    vulnerability_types=?,diseases=?,work_restrictions=?,
                    is_vulnerable=?,notes=?,updated_at=?
                WHERE id=?
            """, (data.get('worker_code',''), data.get('education_date',''),
                  data['name'], data.get('name_korean',''), data['company'],
                  data.get('job_type',''), data.get('nationality',''),
                  data.get('birth_date',''), data.get('birth_year'),
                  data.get('phone',''), data.get('residence_status',''), data.get('residence_expiry',''),
                  data.get('gender',''), data.get('last_exam_date',''),
                  vtypes, data.get('diseases',''), data.get('work_restrictions',''),
                  is_vul, data.get('notes',''), now, worker_id))

    def delete_worker(self, worker_id: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("UPDATE vw_workers SET deleted_at=? WHERE id=?", (now, worker_id))
            con.execute("DELETE FROM vw_health_records WHERE worker_id=?", (worker_id,))
            con.execute("DELETE FROM vw_daily_attendance WHERE worker_id=?", (worker_id,))

    def get_worker(self, worker_id: int) -> Optional[dict]:
        with self.conn() as con:
            row = con.execute("SELECT * FROM vw_workers WHERE id=?", (worker_id,)).fetchone()
            return self._parse_vtypes(dict(row)) if row else None

    # ── Health Exams ──────────────────────────────────────────────────────────
    def get_exams(self, worker_id: int) -> list:
        with self.conn() as con:
            rows = con.execute(
                "SELECT * FROM vw_health_exams WHERE worker_id=? ORDER BY exam_date DESC",
                (worker_id,)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try: d['key_values'] = json.loads(d.get('key_values') or '{}')
                except: d['key_values'] = {}
                result.append(d)
            return result

    def insert_exam(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kv = json.dumps(data.get('key_values', {}), ensure_ascii=False)
        with self.conn() as con:
            cur = con.execute("""
                INSERT INTO vw_health_exams
                    (worker_id,site_code,exam_type,exam_date,key_values,photo_id,notes,created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (data['worker_id'], data['site_code'], data['exam_type'],
                  data['exam_date'], kv, data.get('photo_id',''), data.get('notes',''), now))
            return cur.lastrowid

    def delete_exam(self, exam_id: int) -> str:
        with self.conn() as con:
            row = con.execute("SELECT photo_id FROM vw_health_exams WHERE id=?", (exam_id,)).fetchone()
            con.execute("DELETE FROM vw_health_exams WHERE id=?", (exam_id,))
            return (row[0] or "") if row else ""

    # ── Site Locations ────────────────────────────────────────────────────────
    def get_site_locations(self, site_code: str) -> list:
        with self.conn() as con:
            rows = con.execute(
                "SELECT * FROM vw_site_locations WHERE UPPER(site_code)=UPPER(?) ORDER BY location_name",
                (site_code,)
            ).fetchall()
            return [dict(r) for r in rows]

    def add_site_location(self, site_code: str, name: str) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            cur = con.execute(
                "INSERT INTO vw_site_locations (site_code,location_name,created_at) VALUES (?,?,?)",
                (site_code, name, now)
            )
            return cur.lastrowid

    def delete_site_location(self, loc_id: int):
        with self.conn() as con:
            con.execute("DELETE FROM vw_site_locations WHERE id=?", (loc_id,))

    # ── Company PINs ──────────────────────────────────────────────────────────
    def get_companies_with_pins(self, site_code: str) -> list:
        with self.conn() as con:
            rows = con.execute(
                "SELECT company FROM vw_company_pins WHERE UPPER(site_code)=UPPER(?) ORDER BY company",
                (site_code,)
            ).fetchall()
            return [r[0] for r in rows]

    def verify_company_pin(self, site_code: str, company: str, pin: str) -> bool:
        with self.conn() as con:
            row = con.execute(
                "SELECT pin_hash FROM vw_company_pins WHERE UPPER(site_code)=UPPER(?) AND company=?",
                (site_code, company)
            ).fetchone()
            if not row:
                return False
            if not self._verify(pin, row[0]):
                return False
            # 레거시 해시 자동 업그레이드
            if ":" not in row[0]:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                con.execute(
                    "UPDATE vw_company_pins SET pin_hash=?, updated_at=? "
                    "WHERE UPPER(site_code)=UPPER(?) AND company=?",
                    (self._hash(pin), now, site_code, company),
                )
            return True

    def set_company_pin(self, site_code: str, company: str, pin: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("""
                INSERT OR REPLACE INTO vw_company_pins (site_code,company,pin_hash,updated_at)
                VALUES (UPPER(?),?,?,?)
            """, (site_code, company, self._hash(pin), now))

    def delete_company_pin(self, site_code: str, company: str):
        with self.conn() as con:
            con.execute(
                "DELETE FROM vw_company_pins WHERE UPPER(site_code)=UPPER(?) AND company=?",
                (site_code, company)
            )

    # ── Daily Attendance ──────────────────────────────────────────────────────
    def get_attendance(self, site_code: str, company: str, work_date: str) -> list:
        with self.conn() as con:
            if company:
                rows = con.execute("""
                    SELECT a.*, w.name, w.name_korean, w.birth_date, w.birth_year,
                           w.is_vulnerable, w.vulnerability_types, w.work_restrictions
                    FROM vw_daily_attendance a
                    JOIN vw_workers w ON w.id=a.worker_id
                    WHERE a.site_code=? AND a.company=? AND a.work_date=?
                      AND w.deleted_at IS NULL
                    ORDER BY w.is_vulnerable DESC, COALESCE(NULLIF(w.name_korean,''), w.name)
                """, (site_code, company, work_date)).fetchall()
            else:
                rows = con.execute("""
                    SELECT a.*, w.name, w.name_korean, w.birth_date, w.birth_year,
                           w.is_vulnerable, w.vulnerability_types, w.work_restrictions
                    FROM vw_daily_attendance a
                    JOIN vw_workers w ON w.id=a.worker_id
                    WHERE a.site_code=? AND a.work_date=?
                      AND w.deleted_at IS NULL
                    ORDER BY a.company, w.is_vulnerable DESC, COALESCE(NULLIF(w.name_korean,''), w.name)
                """, (site_code, work_date)).fetchall()
            return [self._parse_vtypes(dict(r)) for r in rows]

    def get_all_attendance_today(self, site_code: str, work_date: str) -> list:
        with self.conn() as con:
            rows = con.execute("""
                SELECT a.*, w.name, w.name_korean, w.birth_date, w.birth_year,
                       w.is_vulnerable, w.vulnerability_types, w.work_restrictions
                FROM vw_daily_attendance a
                JOIN vw_workers w ON w.id=a.worker_id
                WHERE a.site_code=? AND a.work_date=?
                  AND w.deleted_at IS NULL
                ORDER BY a.company, w.is_vulnerable DESC, COALESCE(NULLIF(w.name_korean,''), w.name)
            """, (site_code, work_date)).fetchall()
            return [self._parse_vtypes(dict(r)) for r in rows]

    def set_attendance(self, site_code: str, company: str, work_date: str,
                       worker_ids: list, work_location: str = ""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute(
                "DELETE FROM vw_daily_attendance WHERE site_code=? AND company=? AND work_date=?",
                (site_code, company, work_date)
            )
            for wid in worker_ids:
                con.execute("""
                    INSERT INTO vw_daily_attendance
                        (site_code,company,work_date,worker_id,work_location,created_at)
                    VALUES (?,?,?,?,?,?)
                """, (site_code, company, work_date, wid, work_location, now))

    def get_vulnerable_count(self, site_code: str, company: str, work_date: str) -> int:
        with self.conn() as con:
            row = con.execute("""
                SELECT COUNT(*) FROM vw_daily_attendance a
                JOIN vw_workers w ON w.id=a.worker_id
                WHERE a.site_code=? AND a.company=? AND a.work_date=?
                  AND w.is_vulnerable=1 AND w.deleted_at IS NULL
            """, (site_code, company, work_date)).fetchone()
            return row[0] if row else 0

    # ── Health Records ────────────────────────────────────────────────────────
    def get_health_records(self, site_code: str, company: str,
                           record_date: str, slot: str = "") -> list:
        with self.conn() as con:
            if company:
                q = """
                    SELECT h.*, w.name, w.name_korean, w.is_vulnerable, w.vulnerability_types
                    FROM vw_health_records h
                    JOIN vw_workers w ON w.id=h.worker_id
                    WHERE h.site_code=? AND h.company=? AND h.record_date=?
                """
                p = [site_code, company, record_date]
            else:
                q = """
                    SELECT h.*, w.name, w.name_korean, w.is_vulnerable, w.vulnerability_types
                    FROM vw_health_records h
                    JOIN vw_workers w ON w.id=h.worker_id
                    WHERE h.site_code=? AND h.record_date=?
                """
                p = [site_code, record_date]
            if slot:
                q += " AND h.slot=?"; p.append(slot)
            q += " ORDER BY h.company, w.is_vulnerable DESC, w.name"
            return [self._parse_vtypes(dict(r)) for r in con.execute(q, p).fetchall()]

    def upsert_health_record(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            ex = con.execute("""
                SELECT id FROM vw_health_records
                WHERE record_date=? AND slot=? AND company=? AND worker_id=?
            """, (data['record_date'], data['slot'],
                  data['company'], data['worker_id'])).fetchone()
            if ex:
                con.execute("""
                    UPDATE vw_health_records SET
                        body_temp=?,measure_time=?,health_status=?,notes=?,updated_at=?
                    WHERE id=?
                """, (data.get('body_temp'), data.get('measure_time',''),
                      data.get('health_status','양호'), data.get('notes',''), now, ex[0]))
                return ex[0]
            cur = con.execute("""
                INSERT INTO vw_health_records
                    (site_code,company,record_date,slot,heat_level,feels_like,
                     worker_id,body_temp,measure_time,health_status,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (data['site_code'], data['company'], data['record_date'],
                  data['slot'], data.get('heat_level',''), data.get('feels_like'),
                  data['worker_id'], data.get('body_temp'), data.get('measure_time',''),
                  data.get('health_status','양호'), data.get('notes',''), now, now))
            return cur.lastrowid

    # ── Health Photos ─────────────────────────────────────────────────────────
    def get_health_photos(self, site_code: str, company: str,
                          record_date: str, slot: str) -> list:
        with self.conn() as con:
            rows = con.execute("""
                SELECT * FROM vw_health_photos
                WHERE site_code=? AND company=? AND record_date=? AND slot=?
                ORDER BY photo_type, created_at
            """, (site_code, company, record_date, slot)).fetchall()
            return [dict(r) for r in rows]

    def add_health_photo(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            cur = con.execute("""
                INSERT INTO vw_health_photos
                    (site_code,company,record_date,slot,photo_type,photo_id,created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (data['site_code'], data['company'], data['record_date'],
                  data['slot'], data['photo_type'], data['photo_id'], now))
            return cur.lastrowid

    def delete_health_photo(self, photo_id: str) -> str:
        with self.conn() as con:
            row = con.execute(
                "SELECT photo_id FROM vw_health_photos WHERE photo_id=?", (photo_id,)
            ).fetchone()
            con.execute("DELETE FROM vw_health_photos WHERE photo_id=?", (photo_id,))
            return (row[0] or "") if row else ""
