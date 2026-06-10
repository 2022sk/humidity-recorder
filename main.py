#!/usr/bin/env python3
"""체감온도 기록관리 대장 – FastAPI 서버"""

import io, logging, os, uuid, re, smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("thermohygrometer")

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

from db import Database
from workers_db import WorkersDatabase
from ai import extract_from_image
from excel import build_excel, week_label_ko, get_week_n

load_dotenv(encoding="utf-8")

BASE_DIR   = Path(__file__).parent
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_cors_env = os.environ.get("ALLOWED_ORIGINS", "")
_origins   = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]

app = FastAPI(title="체감온도 기록관리 대장")
app.add_middleware(CORSMiddleware,
                   allow_origins=_origins,
                   allow_methods=["GET","POST","PUT","DELETE"],
                   allow_headers=["Content-Type","Authorization"])

db  = Database(str(DATA_DIR / "data.db"))
wdb = WorkersDatabase(str(DATA_DIR / "data.db"))


def _send_gemini_alert(subject: str, body: str):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    alert_to  = os.environ.get("ALERT_EMAIL_TO", smtp_user)
    if not smtp_user or not smtp_pass or not alert_to:
        logger.warning("이메일 알림 미설정 (SMTP_USER/SMTP_PASS/ALERT_EMAIL_TO): %s", subject)
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = alert_to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info("알림 이메일 발송: %s → %s", subject, alert_to)
    except Exception as e:
        logger.warning("이메일 발송 실패: %s", e)


_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_SLOTS   = {"오전1", "오전2", "오후1", "오후2"}

# ── Pydantic models ───────────────────────────────────────────────────────────
class RecordIn(BaseModel):
    site_code:     str = Field("", max_length=30)
    site_name:     str = Field("", max_length=100)
    company:       str = Field("", max_length=100)
    location:      str = Field("", max_length=100)
    measurer:      str = Field("", max_length=50)
    measure_date:  str
    slot:          str
    week_monday:   str = ""
    measure_time:  str = ""
    temperature:   Optional[float] = Field(None, ge=-50, le=60)
    humidity:      Optional[float] = Field(None, ge=0, le=100)
    feels_like:    Optional[float] = Field(None, ge=-50, le=80)
    heat_level:    str = ""
    action:        str = "N/A"
    other_content: str = ""
    notes:         str = ""
    photo_id:      str = ""

    @field_validator("measure_date", "week_monday", mode="before")
    @classmethod
    def _chk_date(cls, v: str) -> str:
        if v and not _DATE_RE.match(v):
            raise ValueError(f"날짜 형식이 올바르지 않습니다 (YYYY-MM-DD): {v}")
        return v

    @field_validator("slot", mode="before")
    @classmethod
    def _chk_slot(cls, v: str) -> str:
        if v and v not in _SLOTS:
            raise ValueError(f"슬롯은 {_SLOTS} 중 하나여야 합니다")
        return v


# ── 사진 업로드 ───────────────────────────────────────────────────────────────
@app.post("/api/photos/upload")
async def upload_photo(file: UploadFile = File(...)):
    from PIL import Image, ImageOps
    from starlette.concurrency import run_in_threadpool

    content = await file.read()
    filename = file.filename or "photo.jpg"
    photo_id = str(uuid.uuid4())
    save_path = UPLOAD_DIR / f"{photo_id}.jpg"

    def _process():
        img = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        orig_max = max(w, h)
        if orig_max > 1200:
            s = 1200 / orig_max
            img = img.resize((int(w*s), int(h*s)), Image.LANCZOS)
        quality = 60 if orig_max > 3000 else 72
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        save_path.write_bytes(buf.getvalue())

    await run_in_threadpool(_process)
    db.save_photo(photo_id, filename, str(save_path))
    return {"photo_id": photo_id, "filename": filename}


# ── AI 키 관리 ────────────────────────────────────────────────────────────────
class AiKeySetIn(BaseModel):
    site_code: str = Field(..., min_length=1, max_length=30)
    api_key:   str = Field(..., min_length=10)
    pin:       str = ""

@app.get("/api/ai/status")
def ai_status(site_code: str = ""):
    site_has  = db.has_api_key(site_code) if site_code else False
    env_has   = bool(os.environ.get("GEMINI_API_KEY", ""))
    source    = "site" if site_has else ("env" if env_has else "none")
    key_count = len(db.get_api_keys(site_code)) if site_code else 0
    last_err  = wdb.get_last_ai_error(site_code) if site_code else None
    return {"has_key": site_has or env_has, "source": source,
            "key_count": key_count, "last_error": last_err}

@app.get("/api/ai/keys")
def list_ai_keys(site_code: str):
    if not site_code:
        raise HTTPException(400, "site_code required")
    return {"keys": db.get_api_keys_masked(site_code)}

@app.post("/api/ai/key")
def set_ai_key(body: AiKeySetIn):
    if len(body.api_key.strip()) < 10:
        raise HTTPException(400, "올바른 API 키를 입력해 주세요")
    if db.has_pin(body.site_code) and not db.verify_pin(body.site_code, body.pin):
        raise HTTPException(403, "PIN이 올바르지 않습니다")
    db.add_api_key(body.site_code, body.api_key.strip())
    wdb.clear_ai_error(body.site_code)
    logger.info("AI 키 등록: site_code=%s", body.site_code)
    return {"ok": True}

@app.delete("/api/ai/keys/{key_id}")
def delete_ai_key_by_id(key_id: int, site_code: str, pin: str = ""):
    if db.has_pin(site_code) and not db.verify_pin(site_code, pin):
        raise HTTPException(403, "PIN이 올바르지 않습니다")
    db.delete_api_key_by_id(key_id)
    return {"ok": True}

@app.delete("/api/ai/key")
def delete_ai_key(site_code: str, pin: str = ""):
    if db.has_pin(site_code) and not db.verify_pin(site_code, pin):
        raise HTTPException(403, "PIN이 올바르지 않습니다")
    db.delete_api_key(site_code)
    return {"ok": True}

@app.get("/api/ai/usage")
def get_ai_usage(site_code: str = ""):
    today = date.today().isoformat()
    usage = wdb.get_gemini_daily_count(site_code, today)
    daily_limit = int(os.environ.get("GEMINI_DAILY_LIMIT", "150"))
    return {"today": usage, "daily_limit": daily_limit, "warning_at": int(daily_limit * 0.8)}


# ── AI 추출 ───────────────────────────────────────────────────────────────────
@app.post("/api/photos/extract")
async def extract_photo(body: dict):
    photo_id  = body.get("photo_id", "")
    site_code = body.get("site_code", "")
    if not photo_id:
        raise HTTPException(400, "photo_id required")

    # 현장별 키 목록 → 환경변수 순으로 폴백
    api_keys = db.get_api_keys(site_code) if site_code else []
    env_key  = os.environ.get("GEMINI_API_KEY", "")
    if not api_keys and env_key:
        api_keys = [{"key": env_key, "label": "환경변수"}]
    if not api_keys:
        raise HTTPException(500, "AI 키가 등록되지 않았습니다. 상단 AI 버튼을 눌러 키를 등록해 주세요.")

    photo = db.get_photo(photo_id)
    if not photo or not Path(photo["filepath"]).exists():
        raise HTTPException(404, "사진을 찾을 수 없습니다")

    image_bytes = Path(photo["filepath"]).read_bytes()
    daily_limit = int(os.environ.get("GEMINI_DAILY_LIMIT", "150"))

    last_err = None
    all_exhausted = True
    for key_info in api_keys:
        try:
            result = await extract_from_image(image_bytes, key_info["key"])
            logger.info("AI 추출 성공: photo_id=%s label=%s", photo_id, key_info.get("label",""))
            wdb.log_gemini_call(site_code, "success")
            wdb.clear_ai_error(site_code)
            usage = wdb.get_gemini_daily_count()
            success_cnt = usage.get("success", 0)
            if success_cnt >= int(daily_limit * 0.8) and wdb.should_send_alert("daily_warning"):
                wdb.mark_alert_sent("daily_warning")
                _send_gemini_alert(
                    f"[경고] Gemini API 일일 사용량 {success_cnt}/{daily_limit}건",
                    f"오늘 Gemini API 호출이 {success_cnt}건으로 일일 한도({daily_limit})의 80%에 도달했습니다."
                )
            return result
        except Exception as e:
            err = str(e)
            if "RESOURCE_EXHAUSTED" in err or "prepayment" in err.lower() or "quota" in err.lower():
                wdb.log_gemini_call(site_code, "exhausted")
                logger.warning("AI 키[%s] 한도 초과, 다음 키 시도", key_info.get("label",""))
                last_err = e
                continue  # 다음 키 시도
            all_exhausted = False
            last_err = e
            break

    # 모든 키 실패
    logger.warning("AI 추출 실패: photo_id=%s error=%s", photo_id, last_err)
    err = str(last_err)
    if all_exhausted:
        msg = f"모든 키({len(api_keys)}개) 한도 초과"
        wdb.set_ai_error(site_code, msg)
        if wdb.should_send_alert("exhausted"):
            wdb.mark_alert_sent("exhausted")
            _send_gemini_alert("[긴급] Gemini API 크레딧 소진", f"등록된 키 {len(api_keys)}개 모두 한도 초과.\nhttps://aistudio.google.com")
        raise HTTPException(503, "관리자 문의: Gemini API Credit")
    if "API_KEY_INVALID" in err or "API key" in err:
        wdb.set_ai_error(site_code, "키 유효하지 않음")
        wdb.log_gemini_call(site_code, "error", "invalid_key")
        raise HTTPException(401, "Gemini API 키가 유효하지 않습니다. 상단 AI 버튼에서 키를 확인해 주세요.")
    wdb.set_ai_error(site_code, err[:80])
    wdb.log_gemini_call(site_code, "error", err[:50])
    raise HTTPException(502, f"AI 분석 실패: {err[:200]}")


# ── 기록 CRUD ─────────────────────────────────────────────────────────────────
@app.get("/api/records")
def get_records(site_code: str = "", week_monday: str = "", location: str = "", company: str = "", measure_date: str = ""):
    return {"records": db.get_records(site_code=site_code, week_monday=week_monday, location=location, company=company, measure_date=measure_date)}


@app.get("/api/companies")
def get_companies(site_code: str = "", week_monday: str = ""):
    return {"companies": db.get_companies(site_code=site_code, week_monday=week_monday)}


@app.get("/api/heat-summary")
def get_heat_summary(site_code: str, measure_date: str):
    """날짜별 폭염단계 요약: 업체별 슬롯·단계 반환"""
    records = db.get_records(site_code=site_code, measure_date=measure_date)
    NORMAL = {"정상", "", None}
    # 업체별 폭염 슬롯 집계
    by_company: dict = {}
    for r in records:
        co = r.get("company") or "현대건설"
        hl = r.get("heat_level") or ""
        if hl in NORMAL:
            continue
        if co not in by_company:
            by_company[co] = {}
        slot = r.get("slot", "")
        if slot not in by_company[co] or by_company[co][slot]["feels_like"] is None:
            by_company[co][slot] = {"heat_level": hl, "feels_like": r.get("feels_like")}
    companies_with_own = list(by_company.keys())
    hyundai_slots = by_company.get("현대건설", {})
    return {
        "by_company": by_company,
        "companies_with_own": companies_with_own,
        "hyundai_slots": hyundai_slots,
    }


@app.get("/api/locations")
def get_locations(site_code: str = "", company: str = "", week_monday: str = ""):
    return {"locations": db.get_locations(site_code=site_code, company=company, week_monday=week_monday)}


@app.post("/api/records")
def create_record(rec: RecordIn):
    rec_id = db.insert_record(rec.model_dump())
    return {"id": rec_id}


@app.put("/api/records/{rec_id}")
def update_record(rec_id: int, rec: RecordIn):
    db.update_record(rec_id, rec.model_dump())
    return {"id": rec_id}


@app.delete("/api/records/{rec_id}")
def delete_record(rec_id: int):
    db.delete_record(rec_id)   # 소프트 삭제 — 사진은 유지
    return {"ok": True}


# ── 휴지통 ──────────────────────────────────────────────────────────────────
@app.get("/api/trash")
def get_trash(site_code: str = ""):
    return {"records": db.get_trash(site_code=site_code)}


@app.post("/api/trash/{rec_id}/restore")
def restore_trash(rec_id: int):
    db.restore_record(rec_id)
    return {"ok": True}


@app.delete("/api/trash/{rec_id}")
def purge_trash_record(rec_id: int):
    photo_id = db.purge_record(rec_id)          # records만 삭제
    if photo_id:
        photo = db.get_photo(photo_id)           # 파일 경로 조회 (photos는 아직 존재)
        if photo:
            Path(photo["filepath"]).unlink(missing_ok=True)
        db.delete_photo(photo_id)                # 파일 삭제 후 photos 삭제
    return {"ok": True}


@app.delete("/api/trash")
def empty_trash(site_code: str = ""):
    photo_ids = db.purge_all_trash(site_code=site_code)  # records만 삭제
    for pid in photo_ids:
        photo = db.get_photo(pid)                         # 파일 경로 조회
        if photo:
            Path(photo["filepath"]).unlink(missing_ok=True)
        db.delete_photo(pid)                              # 파일 삭제 후 photos 삭제
    return {"ok": True, "purged": len(photo_ids)}


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: str):
    if not re.fullmatch(r"[0-9a-f\-]{36}", photo_id):
        raise HTTPException(400, "잘못된 photo_id")
    photo = db.get_photo(photo_id)
    if photo:
        fp = Path(photo["filepath"])
        if fp.exists():
            fp.unlink(missing_ok=True)
        db.delete_photo(photo_id)
    return {"ok": True}


# ── PIN 관리 ──────────────────────────────────────────────────────────────────
class PinVerifyIn(BaseModel):
    site_code: str
    pin: str

class PinSetIn(BaseModel):
    site_code: str
    current_pin: str = ""
    new_pin: str

@app.get("/api/pin/status")
def pin_status(site_code: str = ""):
    if not site_code:
        raise HTTPException(400, "site_code required")
    return {"has_pin": db.has_pin(site_code)}

@app.post("/api/pin/verify")
def verify_pin(body: PinVerifyIn):
    return {"ok": db.verify_pin(body.site_code, body.pin)}

@app.post("/api/pin/set")
def set_pin(body: PinSetIn):
    if not body.new_pin or len(body.new_pin.strip()) < 4:
        raise HTTPException(400, "PIN은 4자리 이상이어야 합니다")
    if db.has_pin(body.site_code) and not db.verify_pin(body.site_code, body.current_pin):
        raise HTTPException(403, "현재 PIN이 올바르지 않습니다")
    db.set_pin(body.site_code, body.new_pin)
    return {"ok": True}


# ── 자동완성 / 이력 ────────────────────────────────────────────────────────────
@app.get("/api/autocomplete")
def get_autocomplete(site_code: str = ""):
    return db.get_autocomplete(site_code=site_code)


@app.get("/api/weeks")
def get_available_weeks(site_code: str = ""):
    return {"weeks": db.get_available_weeks(site_code=site_code)}


# ── 엑셀 다운로드 ──────────────────────────────────────────────────────────────
@app.get("/api/excel")
def download_excel(site_code: str = "", week_monday: str = "", location: str = "", company: str = ""):
    records = db.get_records(site_code=site_code, week_monday=week_monday, location=location, company=company)
    if not records:
        raise HTTPException(404, "해당 기간의 기록이 없습니다")

    rec0 = records[0]
    meta = {
        "현장명":   rec0.get("site_name",""),
        "업체명":   company or rec0.get("company",""),
        "위치":     location or rec0.get("location",""),
        "현장코드": rec0.get("site_code",""),
    }

    for r in records:
        if r.get("photo_id"):
            photo = db.get_photo(r["photo_id"])
            if photo:
                fp = Path(photo["filepath"])
                if fp.exists():
                    r["_bytes"] = fp.read_bytes()

    try:
        monday_date = date.fromisoformat(week_monday)
    except Exception:
        monday_date = date.today()

    excel_bytes = build_excel(records, meta, monday_date)

    wk_n = get_week_n(monday_date)
    loc  = location or rec0.get("location","위치미정")
    corp = company or rec0.get("company","")
    code = rec0.get("site_code","")
    prefix = f"({code})" if code else ""
    corp_part = f"_{corp}" if corp else ""
    fname = f"{prefix}체감온도기록관리대장_{loc}{corp_part}_{monday_date.year}년{monday_date.month}월{week_label_ko(wk_n)}.xlsx"

    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


# ── 사진 서빙 ─────────────────────────────────────────────────────────────────
@app.get("/uploads/{photo_id}")
def serve_photo(photo_id: str):
    # 경로 탐색 공격 방지: UUID 형식만 허용
    if not re.fullmatch(r"[0-9a-f\-]{36}", photo_id):
        raise HTTPException(400, "잘못된 photo_id")
    path = UPLOAD_DIR / f"{photo_id}.jpg"
    if path.exists():
        return FileResponse(str(path), media_type="image/jpeg")
    raise HTTPException(404, "사진 없음")


# ══ 취약근로자 관리 API ═══════════════════════════════════════════════════════

# ── Pydantic models ───────────────────────────────────────────────────────────
class WorkerIn(BaseModel):
    site_code: str
    worker_code: str = ""
    education_date: str = ""
    name: str
    name_korean: str = ""
    company: str
    job_type: str = ""
    nationality: str = ""
    birth_date: str = ""
    birth_year: Optional[int] = None
    phone: str = ""
    residence_status: str = ""
    residence_expiry: str = ""
    gender: str = ""
    last_exam_date: str = ""
    vulnerability_types: list = []
    diseases: str = ""
    work_restrictions: str = ""
    notes: str = ""

class ExamIn(BaseModel):
    worker_id: int
    site_code: str
    exam_type: str
    exam_date: str
    key_values: dict = {}
    photo_id: str = ""
    notes: str = ""

class AttendanceIn(BaseModel):
    site_code: str
    company: str
    work_date: str
    worker_ids: list = []
    work_location: str = ""
    workers: list = []

class HealthRecordIn(BaseModel):
    site_code: str
    company: str
    record_date: str
    slot: str
    heat_level: str = ""
    feels_like: Optional[float] = None
    worker_id: int
    body_temp: Optional[float] = None
    measure_time: str = ""
    health_status: str = "양호"
    notes: str = ""

class HealthPhotoIn(BaseModel):
    site_code: str
    company: str
    record_date: str
    slot: str
    photo_type: str
    photo_id: str

class CompanyPinSetIn(BaseModel):
    site_code: str
    company: str
    pin: str
    admin_pin: str = ""

class CompanyPinVerifyIn(BaseModel):
    site_code: str
    company: str
    pin: str

class CompanyVisibilityIn(BaseModel):
    site_code: str
    company: str
    visible: bool

class DeployStatusIn(BaseModel):
    status: str

# ── 업체 가시성 관리 ──────────────────────────────────────────────────────────
@app.get("/api/vw/company-visibility")
def get_company_visibility(site_code: str):
    vis = wdb.get_company_visibility(site_code)
    return {"visibility": vis}

@app.post("/api/vw/company-visibility")
def set_company_visibility(body: CompanyVisibilityIn):
    wdb.set_company_visibility(body.site_code, body.company, body.visible)
    return {"ok": True}

@app.get("/api/vw/visible-companies")
def get_visible_companies(site_code: str):
    return {"companies": wdb.get_visible_companies(site_code)}

# ── 미투입 상태 관리 ──────────────────────────────────────────────────────────
@app.put("/api/vw/workers/{worker_id}/deploy-status")
def update_deploy_status(worker_id: int, body: DeployStatusIn):
    if body.status not in ("active", "inactive"):
        raise HTTPException(400, "status must be active or inactive")
    wdb.update_worker_deploy_status(worker_id, body.status)
    return {"ok": True}

# ── /workers 페이지 ───────────────────────────────────────────────────────────
@app.get("/workers")
def serve_workers():
    resp = FileResponse(str(STATIC_DIR / "workers.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.get("/workers/{site_code}")
def serve_workers_site(site_code: str):
    resp = FileResponse(str(STATIC_DIR / "workers.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ── 근로자 관리 ───────────────────────────────────────────────────────────────
@app.get("/api/vw/workers")
def get_workers(site_code: str, company: str = ""):
    # 하루 한 번 신규배치자·고령 자동 재계산
    if site_code and wdb.needs_daily_recalc(site_code):
        _auto_recalc(site_code)
        wdb.mark_daily_recalc(site_code)
    return {"workers": wdb.get_workers(site_code, company)}

@app.post("/api/vw/workers")
def create_worker(body: WorkerIn):
    wid = wdb.insert_worker(body.model_dump())
    return {"id": wid}

class WorkerBulkIn(BaseModel):
    site_code: str
    workers: list

@app.post("/api/vw/workers/bulk")
def bulk_create_workers(body: WorkerBulkIn):
    success, errors = 0, []
    for w in body.workers:
        try:
            w["site_code"] = body.site_code
            if not w.get("name"):
                w["name"] = w.get("name_korean", "미입력")
            if not w.get("company"):
                w["company"] = "미지정"
            wdb.insert_worker(w)
            success += 1
        except Exception as e:
            errors.append(str(e))
    return {"success": success, "errors": errors}

@app.put("/api/vw/workers/{worker_id}")
def update_worker(worker_id: int, body: WorkerIn):
    wdb.update_worker(worker_id, body.model_dump())
    return {"ok": True}

@app.delete("/api/vw/workers/{worker_id}")
def delete_worker(worker_id: int):
    wdb.delete_worker(worker_id)
    return {"ok": True}

# ── 건강진단 ──────────────────────────────────────────────────────────────────
@app.get("/api/vw/workers/{worker_id}/exams")
def get_exams(worker_id: int):
    return {"exams": wdb.get_exams(worker_id)}

@app.post("/api/vw/exams")
def create_exam(body: ExamIn):
    eid = wdb.insert_exam(body.model_dump())
    return {"id": eid}

@app.delete("/api/vw/exams/{exam_id}")
def delete_exam(exam_id: int):
    photo_id = wdb.delete_exam(exam_id)
    if photo_id:
        photo = db.get_photo(photo_id)
        if photo:
            fp = Path(photo["filepath"])
            if fp.exists():
                fp.unlink(missing_ok=True)
    return {"ok": True}

# ── 현장 위치 ─────────────────────────────────────────────────────────────────
@app.get("/api/vw/locations")
def get_vw_locations(site_code: str):
    return {"locations": wdb.get_site_locations(site_code)}

@app.post("/api/vw/locations")
def add_vw_location(body: dict):
    lid = wdb.add_site_location(body["site_code"], body["name"])
    return {"id": lid}

@app.delete("/api/vw/locations/{loc_id}")
def del_vw_location(loc_id: int):
    wdb.delete_site_location(loc_id)
    return {"ok": True}

@app.patch("/api/vw/locations/{loc_id}")
def rename_vw_location(loc_id: int, body: dict):
    if "name" in body:
        wdb.rename_site_location(loc_id, body["name"])
    if "color" in body:
        wdb.set_location_color(loc_id, body["color"])
    return {"ok": True}

# ── 업체 PIN ──────────────────────────────────────────────────────────────────
@app.get("/api/vw/company-pins")
def get_company_pins(site_code: str):
    return {"companies": wdb.get_companies_with_pins(site_code)}

@app.post("/api/vw/company-pins/verify")
def verify_company_pin(body: CompanyPinVerifyIn):
    return {"ok": wdb.verify_company_pin(body.site_code, body.company, body.pin)}

@app.post("/api/vw/company-pins/set")
def set_company_pin(body: CompanyPinSetIn):
    if not db.verify_pin(body.site_code, body.admin_pin):
        raise HTTPException(403, "관리자 PIN이 올바르지 않습니다")
    if len(body.pin.strip()) < 4:
        raise HTTPException(400, "PIN은 4자리 이상이어야 합니다")
    wdb.set_company_pin(body.site_code, body.company, body.pin)
    return {"ok": True}

@app.delete("/api/vw/company-pins")
def del_company_pin(site_code: str, company: str):
    wdb.delete_company_pin(site_code, company)
    return {"ok": True}

# ── 취약근로자 날짜별 로그 ──────────────────────────────────────────────────────
@app.post("/api/vw/vuln-log")
def save_vuln_log(body: dict):
    wdb.save_vuln_log(body["site_code"], body["log_date"], body.get("data", []))
    return {"ok": True}

@app.get("/api/vw/vuln-log")
def get_vuln_log(site_code: str, log_date: str):
    return {"data": wdb.get_vuln_log(site_code, log_date)}

@app.get("/api/vw/vuln-log/dates")
def list_vuln_log_dates(site_code: str):
    return {"dates": wdb.list_vuln_log_dates(site_code)}

def _auto_recalc(site_code: str):
    """신규배치자·고령 자동 재계산 (내부용)"""
    from datetime import date, datetime as dt2
    today = date.today()
    AGE_LABELS = {'고령(만60세이상)', '초고령(만66세이상)', '고령', '초고령'}
    for w in wdb.get_workers(site_code):
        vtypes = [v for v in (w.get('vulnerability_types') or [])
                  if v not in AGE_LABELS and v != '신규배치자']
        bd = w.get('birth_date', '')
        if bd:
            try:
                by, bm, bday = (int(x) for x in bd.split('-'))
                age = today.year - by - (1 if (today.month, today.day) < (bm, bday) else 0)
                if age >= 66: vtypes.append('초고령')
                elif age >= 60: vtypes.append('고령')
            except Exception:
                pass
        edu = w.get('education_date', '')
        if edu:
            try:
                edu_date = dt2.strptime(edu[:10], '%Y-%m-%d').date()
                if 0 <= (today - edu_date).days <= 14:
                    vtypes.append('신규배치자')
            except Exception:
                pass
        w['vulnerability_types'] = vtypes
        w['is_vulnerable'] = 1 if vtypes else 0
        wdb.update_worker(w['id'], w)

# ── 취약구분 일괄 재계산 ───────────────────────────────────────────────────────
@app.post("/api/vw/workers/recalc-vtypes")
def recalc_vtypes(site_code: str):
    from datetime import date
    today = date.today()
    workers = wdb.get_workers(site_code)
    updated = 0
    AGE_LABELS = {'고령(만60세이상)', '초고령(만66세이상)', '고령', '초고령'}
    for w in workers:
        # 나이·신규배치자 재계산, 질환 기반·수동 체크는 저장된 값 유지
        vtypes = [v for v in (w.get('vulnerability_types') or [])
                  if v not in AGE_LABELS and v != '신규배치자']
        bd = w.get('birth_date', '')
        if bd:
            try:
                by, bm, bday = (int(x) for x in bd.split('-'))
                age = today.year - by - (1 if (today.month, today.day) < (bm, bday) else 0)
                if age >= 66: vtypes.append('초고령')
                elif age >= 60: vtypes.append('고령')
            except Exception:
                pass
        edu = w.get('education_date', '')
        if edu:
            try:
                from datetime import datetime
                edu_date = datetime.strptime(edu[:10], '%Y-%m-%d').date()
                diff = (today - edu_date).days
                if 0 <= diff <= 14: vtypes.append('신규배치자')
            except Exception:
                pass
        w['vulnerability_types'] = vtypes
        w['is_vulnerable'] = 1 if vtypes else 0
        wdb.update_worker(w['id'], w)
        updated += 1
    return {"ok": True, "updated": updated}

# ── 출근 체크 ─────────────────────────────────────────────────────────────────
@app.get("/api/vw/attendance")
def get_attendance(site_code: str, company: str = '', work_date: str = ''):
    return {"attendance": wdb.get_attendance(site_code, company, work_date)}

@app.post("/api/vw/attendance")
def set_attendance(body: AttendanceIn):
    wdb.set_attendance(body.site_code, body.company, body.work_date,
                       body.worker_ids, body.work_location,
                       workers=body.workers if body.workers else None)
    return {"ok": True}

@app.get("/api/vw/attendance/vulnerable")
def get_vulnerable(site_code: str, company: str, work_date: str):
    count = wdb.get_vulnerable_count(site_code, company, work_date)
    att   = wdb.get_attendance(site_code, company, work_date)
    vuln  = [a for a in att if a.get('is_vulnerable')]
    return {"count": count, "workers": vuln}

# ── 건강기록 ──────────────────────────────────────────────────────────────────
@app.get("/api/vw/health-records")
def get_health_records(site_code: str, company: str = '', record_date: str = '', slot: str = ""):
    return {"records": wdb.get_health_records(site_code, company, record_date, slot)}

@app.post("/api/vw/health-records")
def upsert_health_record(body: HealthRecordIn):
    rid = wdb.upsert_health_record(body.model_dump())
    return {"id": rid}

@app.get("/api/vw/health-photos")
def get_health_photos(site_code: str, company: str, record_date: str, slot: str):
    return {"photos": wdb.get_health_photos(site_code, company, record_date, slot)}

@app.post("/api/vw/health-photos")
def add_health_photo(body: HealthPhotoIn):
    pid = wdb.add_health_photo(body.model_dump())
    return {"id": pid}

@app.delete("/api/vw/health-photos/{photo_id}")
def del_health_photo(photo_id: str):
    if not re.fullmatch(r"[0-9a-f\-]{36}", photo_id):
        raise HTTPException(400, "잘못된 photo_id")
    file_id = wdb.delete_health_photo(photo_id)
    if file_id:
        photo = db.get_photo(file_id)
        if photo:
            fp = Path(photo["filepath"])
            if fp.exists():
                fp.unlink(missing_ok=True)
        db.delete_photo(file_id)
    return {"ok": True}


# ── 프론트엔드 서빙 (마지막에 마운트) ──────────────────────────────────────────
@app.get("/")
def serve_index():
    resp = FileResponse(str(STATIC_DIR / "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.get("/site_map.png")
def serve_site_map():
    import os as _os
    path = str(STATIC_DIR / "site_map.png")
    mtime = int(_os.path.getmtime(path))
    resp = FileResponse(path, media_type="image/png")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["ETag"] = str(mtime)
    return resp

@app.get("/{site_code}")
def serve_site(site_code: str):
    resp = FileResponse(str(STATIC_DIR / "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=_port, reload=False)
