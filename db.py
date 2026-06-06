"""SQLite 데이터베이스 레이어"""

import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional


class Database:
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

    _RECORDS_DDL = """\
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
            updated_at    TEXT    DEFAULT (datetime('now','localtime'))
        )"""

    def _add_columns_if_missing(self, con):
        cols = {row[1] for row in con.execute("PRAGMA table_info(records)")}
        if 'deleted_at' not in cols:
            con.execute("ALTER TABLE records ADD COLUMN deleted_at TEXT DEFAULT NULL")

    def _auto_purge(self, con) -> list:
        """30일 이상 지난 소프트삭제 기록 영구 삭제. 삭제할 파일 경로 목록 반환."""
        rows = con.execute(
            "SELECT r.id, p.filepath FROM records r "
            "LEFT JOIN photos p ON p.id=r.photo_id AND r.photo_id!='' "
            "WHERE r.deleted_at IS NOT NULL "
            "AND r.deleted_at < datetime('now','-30 days','localtime')"
        ).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        filepaths = [r[1] for r in rows if r[1]]
        ph = ','.join('?' * len(ids))
        photo_ids = [r[0] for r in con.execute(
            f"SELECT photo_id FROM records WHERE id IN ({ph}) AND photo_id!=''", ids
        ).fetchall()]
        con.execute(f"DELETE FROM records WHERE id IN ({ph})", ids)
        if photo_ids:
            ph2 = ','.join('?' * len(photo_ids))
            con.execute(f"DELETE FROM photos WHERE id IN ({ph2})", photo_ids)
        return filepaths

    def _needs_migration(self, con) -> bool:
        """UNIQUE 제약이 남아있는지 두 가지 방법으로 확인."""
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        if row and "UNIQUE" in (row[0] or "").upper():
            return True
        idx = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='records' "
            "AND name LIKE 'sqlite_autoindex_%'"
        ).fetchone()
        return idx is not None

    def _init(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL")
        try:
            con.execute("BEGIN")
            # 이전 마이그레이션이 중단된 경우(_records_bak 잔류) 재개
            bak = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_records_bak'"
            ).fetchone()
            if bak:
                con.execute("DROP TABLE IF EXISTS records")
                con.execute(self._RECORDS_DDL)
                con.execute("INSERT INTO records SELECT * FROM _records_bak")
                con.execute("DROP TABLE _records_bak")
            elif self._needs_migration(con):
                con.execute("DROP TABLE IF EXISTS _records_bak")
                con.execute("ALTER TABLE records RENAME TO _records_bak")
                con.execute(self._RECORDS_DDL)
                con.execute("INSERT INTO records SELECT * FROM _records_bak")
                con.execute("DROP TABLE _records_bak")
            con.execute("""
                CREATE TABLE IF NOT EXISTS photos (
                    id       TEXT PRIMARY KEY,
                    filename TEXT,
                    filepath TEXT,
                    uploaded_at TEXT DEFAULT (datetime('now','localtime'))
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS site_pins (
                    site_code  TEXT PRIMARY KEY,
                    pin_hash   TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                )""")
            con.execute(self._RECORDS_DDL)
            con.execute("CREATE INDEX IF NOT EXISTS idx_records_site_week ON records(site_code, week_monday)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_records_site_date ON records(site_code, measure_date)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS site_api_keys (
                    site_code  TEXT PRIMARY KEY,
                    api_key_enc TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                )""")
            self._add_columns_if_missing(con)
            filepaths = self._auto_purge(con)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        for fp in filepaths:
            try:
                Path(fp).unlink(missing_ok=True)
            except Exception:
                pass

    # ── AI Key 관리 ───────────────────────────────────────────────────────────
    @staticmethod
    def _xor(text: str) -> str:
        """서버 비밀키로 XOR 암호화/복호화 (대칭)."""
        secret = os.environ.get("SECRET_KEY", "thermohygrometer-2026")
        key = hashlib.sha256(secret.encode()).digest()
        data = text.encode()
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).hex()

    @staticmethod
    def _unxor(hex_str: str) -> str:
        secret = os.environ.get("SECRET_KEY", "thermohygrometer-2026")
        key = hashlib.sha256(secret.encode()).digest()
        data = bytes.fromhex(hex_str)
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode()

    def set_api_key(self, site_code: str, api_key: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        enc = self._xor(api_key)
        with self.conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO site_api_keys (site_code, api_key_enc, updated_at) "
                "VALUES (UPPER(?), ?, ?)",
                (site_code, enc, now),
            )

    def get_api_key(self, site_code: str) -> Optional[str]:
        with self.conn() as con:
            row = con.execute(
                "SELECT api_key_enc FROM site_api_keys WHERE UPPER(site_code)=UPPER(?)",
                (site_code,),
            ).fetchone()
            if not row:
                return None
            try:
                return self._unxor(row[0])
            except Exception:
                return None

    def has_api_key(self, site_code: str) -> bool:
        with self.conn() as con:
            row = con.execute(
                "SELECT 1 FROM site_api_keys WHERE UPPER(site_code)=UPPER(?)",
                (site_code,),
            ).fetchone()
            return row is not None

    def delete_api_key(self, site_code: str):
        with self.conn() as con:
            con.execute(
                "DELETE FROM site_api_keys WHERE UPPER(site_code)=UPPER(?)",
                (site_code,),
            )

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
    def get_records(self, site_code: str = "", week_monday: str = "", location: str = "", company: str = "", measure_date: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT * FROM records WHERE deleted_at IS NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            if measure_date:
                q += " AND measure_date=?"; p.append(measure_date)
            if location:
                q += " AND location=?"; p.append(location)
            if company:
                q += " AND company=?"; p.append(company)
            q += " ORDER BY measure_date, slot, id"
            return [dict(r) for r in con.execute(q, p).fetchall()]

    def get_companies(self, site_code: str = "", week_monday: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT company FROM records WHERE company!='' AND deleted_at IS NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            q += " ORDER BY company"
            return [r[0] for r in con.execute(q, p).fetchall()]

    def get_locations(self, site_code: str = "", company: str = "", week_monday: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT location FROM records WHERE location!='' AND deleted_at IS NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            if company:
                q += " AND company=?"; p.append(company)
            if week_monday:
                q += " AND week_monday=?"; p.append(week_monday)
            q += " ORDER BY location"
            return [r[0] for r in con.execute(q, p).fetchall()]

    def insert_record(self, data: dict) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            cur = con.execute("""
                INSERT INTO records (
                    site_code,site_name,company,location,measurer,
                    measure_date,slot,week_monday,measure_time,
                    temperature,humidity,feels_like,heat_level,
                    action,other_content,notes,photo_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data.get("site_code",""), data.get("site_name",""),
                data.get("company",""),   data.get("location",""),
                data.get("measurer",""),  data["measure_date"],
                data["slot"],             data.get("week_monday",""),
                data.get("measure_time",""),
                data.get("temperature"),  data.get("humidity"),
                data.get("feels_like"),   data.get("heat_level",""),
                data.get("action","N/A"), data.get("other_content",""),
                data.get("notes",""),     data.get("photo_id",""), now, now,
            ))
            return cur.lastrowid

    def update_record(self, rec_id: int, data: dict):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("""
                UPDATE records SET
                    site_code=?,site_name=?,company=?,location=?,measurer=?,
                    measure_time=?,temperature=?,humidity=?,feels_like=?,
                    heat_level=?,action=?,other_content=?,notes=?,photo_id=?,updated_at=?
                WHERE id=?
            """, (
                data.get("site_code",""), data.get("site_name",""),
                data.get("company",""),   data.get("location",""),
                data.get("measurer",""),  data.get("measure_time",""),
                data.get("temperature"),  data.get("humidity"),
                data.get("feels_like"),   data.get("heat_level",""),
                data.get("action","N/A"), data.get("other_content",""),
                data.get("notes",""),     data.get("photo_id",""),
                now, rec_id,
            ))

    def get_record_photo_id(self, rec_id: int) -> str:
        with self.conn() as con:
            row = con.execute("SELECT photo_id FROM records WHERE id=?", (rec_id,)).fetchone()
            return row[0] or "" if row else ""

    def delete_record(self, rec_id: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute("UPDATE records SET deleted_at=? WHERE id=?", (now, rec_id))

    # ── 휴지통 ────────────────────────────────────────────────────────────────
    def get_trash(self, site_code: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT * FROM records WHERE deleted_at IS NOT NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            q += " ORDER BY deleted_at DESC"
            return [dict(r) for r in con.execute(q, p).fetchall()]

    def restore_record(self, rec_id: int):
        with self.conn() as con:
            con.execute("UPDATE records SET deleted_at=NULL WHERE id=?", (rec_id,))

    def purge_record(self, rec_id: int) -> str:
        """records만 영구 삭제. photo_id 반환 — photos 삭제·파일 삭제는 호출자가 처리."""
        with self.conn() as con:
            row = con.execute("SELECT photo_id FROM records WHERE id=?", (rec_id,)).fetchone()
            photo_id = (row[0] or "") if row else ""
            con.execute("DELETE FROM records WHERE id=?", (rec_id,))
        return photo_id

    def purge_all_trash(self, site_code: str = "") -> list:
        """records만 전체 영구 삭제. photo_id 목록 반환 — photos 삭제·파일 삭제는 호출자가 처리."""
        with self.conn() as con:
            q, p = "SELECT id, photo_id FROM records WHERE deleted_at IS NOT NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            rows = con.execute(q, p).fetchall()
            if not rows:
                return []
            ids = [r[0] for r in rows]
            photo_ids = [r[1] for r in rows if r[1]]
            ph = ','.join('?' * len(ids))
            con.execute(f"DELETE FROM records WHERE id IN ({ph})", ids)
        return photo_ids

    # ── PIN 관리 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _hash(pin: str) -> str:
        """pbkdf2 해싱. 반환 형식: salt_hex:dk_hex"""
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pin.strip().encode(), salt, 260_000)
        return salt.hex() + ":" + dk.hex()

    @staticmethod
    def _verify(pin: str, stored: str) -> bool:
        """저장된 해시 검증. 구형 SHA-256(salt 없음)과 신형 pbkdf2 모두 지원."""
        pin_bytes = pin.strip().encode()
        if ":" in stored:  # 신형 pbkdf2
            try:
                salt_hex, dk_hex = stored.split(":", 1)
                salt = bytes.fromhex(salt_hex)
                dk = hashlib.pbkdf2_hmac("sha256", pin_bytes, salt, 260_000)
                return hmac.compare_digest(dk.hex(), dk_hex)
            except Exception:
                return False
        else:  # 구형 SHA-256 (레거시)
            legacy = hashlib.sha256(pin_bytes).hexdigest()
            return hmac.compare_digest(legacy, stored)

    def has_pin(self, site_code: str) -> bool:
        with self.conn() as con:
            row = con.execute(
                "SELECT 1 FROM site_pins WHERE UPPER(site_code)=UPPER(?)", (site_code,)
            ).fetchone()
            return row is not None

    @staticmethod
    def _default_pin(site_code: str) -> str:
        """현장코드에서 숫자만 추출 후 앞에 0을 붙여 4자리로 만든 기본 PIN."""
        digits = ''.join(c for c in site_code if c.isdigit())
        return digits.zfill(4)

    def verify_pin(self, site_code: str, pin: str) -> bool:
        with self.conn() as con:
            row = con.execute(
                "SELECT pin_hash FROM site_pins WHERE UPPER(site_code)=UPPER(?)", (site_code,)
            ).fetchone()
            if not row:
                return pin.strip() == self._default_pin(site_code)
            if not self._verify(pin, row[0]):
                return False
            # 레거시 SHA-256 해시면 pbkdf2로 자동 업그레이드
            if ":" not in row[0]:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                con.execute(
                    "UPDATE site_pins SET pin_hash=?, updated_at=? WHERE UPPER(site_code)=UPPER(?)",
                    (self._hash(pin), now, site_code),
                )
            return True

    def set_pin(self, site_code: str, pin: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO site_pins (site_code, pin_hash, updated_at) VALUES (UPPER(?),?,?)",
                (site_code, self._hash(pin), now),
            )

    # ── Autocomplete / History ─────────────────────────────────────────────────
    def get_autocomplete(self, site_code: str = "") -> dict:
        with self.conn() as con:
            codes = con.execute(
                "SELECT DISTINCT site_code,site_name FROM records "
                "WHERE site_code!='' AND deleted_at IS NULL ORDER BY site_code"
            ).fetchall()
            if site_code:
                companies = con.execute(
                    "SELECT DISTINCT company FROM records "
                    "WHERE company!='' AND deleted_at IS NULL AND UPPER(site_code)=UPPER(?) ORDER BY company",
                    (site_code,)
                ).fetchall()
                locations = con.execute(
                    "SELECT DISTINCT location FROM records "
                    "WHERE location!='' AND deleted_at IS NULL AND UPPER(site_code)=UPPER(?) ORDER BY location",
                    (site_code,)
                ).fetchall()
            else:
                companies = con.execute(
                    "SELECT DISTINCT company FROM records WHERE company!='' AND deleted_at IS NULL ORDER BY company"
                ).fetchall()
                locations = con.execute(
                    "SELECT DISTINCT location FROM records WHERE location!='' AND deleted_at IS NULL ORDER BY location"
                ).fetchall()
        return {
            "site_codes": [{"code": r[0], "name": r[1]} for r in codes],
            "companies":  [r[0] for r in companies],
            "locations":  [r[0] for r in locations],
        }

    def get_available_weeks(self, site_code: str = "") -> list:
        with self.conn() as con:
            q, p = "SELECT DISTINCT week_monday FROM records WHERE week_monday!='' AND deleted_at IS NULL", []
            if site_code:
                q += " AND UPPER(site_code)=UPPER(?)"; p.append(site_code)
            q += " ORDER BY week_monday DESC"
            return [r[0] for r in con.execute(q, p).fetchall()]
