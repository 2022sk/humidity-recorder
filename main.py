#!/usr/bin/env python3
"""체감온도 기록관리 대장 – FastAPI 서버"""

import io, os, uuid, re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from db import Database
from ai import extract_from_image
from excel import build_excel, week_label_ko, get_week_n

load_dotenv(encoding="utf-8")

BASE_DIR   = Path(__file__).parent
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="체감온도 기록관리 대장")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

db = Database(str(DATA_DIR / "data.db"))


# ── Pydantic models ───────────────────────────────────────────────────────────
class RecordIn(BaseModel):
    site_code:     str = ""
    site_name:     str = ""
    company:       str = ""
    location:      str = ""
    measurer:      str = ""
    measure_date:  str          # YYYY-MM-DD
    slot:          str          # 오전1/오전2/오후1/오후2
    week_monday:   str = ""     # YYYY-MM-DD
    measure_time:  str = ""
    temperature:   Optional[float] = None
    humidity:      Optional[float] = None
    feels_like:    Optional[float] = None
    heat_level:    str = ""
    action:        str = "N/A"
    other_content: str = ""
    notes:         str = ""
    photo_id:      str = ""


# ── 사진 업로드 ───────────────────────────────────────────────────────────────
@app.post("/api/photos/upload")
async def upload_photo(file: UploadFile = File(...)):
    from PIL import Image, ImageOps

    content = await file.read()
    photo_id = str(uuid.uuid4())
    save_path = UPLOAD_DIR / f"{photo_id}.jpg"

    # EXIF 회전 보정 + 리사이즈
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    if max(w, h) > 1568:
        s = 1568 / max(w, h)
        img = img.resize((int(w*s), int(h*s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    save_path.write_bytes(buf.getvalue())

    db.save_photo(photo_id, file.filename or "photo.jpg", str(save_path))
    return {"photo_id": photo_id, "filename": file.filename}


# ── AI 추출 ───────────────────────────────────────────────────────────────────
@app.post("/api/photos/extract")
async def extract_photo(body: dict):
    photo_id = body.get("photo_id","")
    if not photo_id:
        raise HTTPException(400, "photo_id required")

    api_key = os.environ.get("GEMINI_API_KEY","")
    if not api_key:
        raise HTTPException(500, "GEMINI_API_KEY 미설정")

    photo = db.get_photo(photo_id)
    if not photo or not Path(photo["filepath"]).exists():
        raise HTTPException(404, "사진을 찾을 수 없습니다")

    image_bytes = Path(photo["filepath"]).read_bytes()
    result = await extract_from_image(image_bytes, api_key)
    return result


# ── 기록 CRUD ─────────────────────────────────────────────────────────────────
@app.get("/api/records")
def get_records(site_code: str = "", week_monday: str = ""):
    return {"records": db.get_records(site_code=site_code, week_monday=week_monday)}


@app.post("/api/records")
def upsert_record(rec: RecordIn):
    rec_id = db.upsert_record(rec.model_dump())
    return {"id": rec_id}


@app.put("/api/records/{rec_id}")
def update_record(rec_id: int, rec: RecordIn):
    db.update_record(rec_id, rec.model_dump())
    return {"id": rec_id}


@app.delete("/api/records/{rec_id}")
def delete_record(rec_id: int):
    db.delete_record(rec_id)
    return {"ok": True}


# ── 자동완성 / 이력 ────────────────────────────────────────────────────────────
@app.get("/api/autocomplete")
def get_autocomplete():
    return db.get_autocomplete()


@app.get("/api/weeks")
def get_available_weeks(site_code: str = ""):
    return {"weeks": db.get_available_weeks(site_code=site_code)}


# ── 엑셀 다운로드 ──────────────────────────────────────────────────────────────
@app.get("/api/excel")
def download_excel(site_code: str = "", week_monday: str = ""):
    records = db.get_records(site_code=site_code, week_monday=week_monday)
    if not records:
        raise HTTPException(404, "해당 기간의 기록이 없습니다")

    rec0 = records[0]
    meta = {
        "현장명": rec0.get("site_name",""),
        "업체명": rec0.get("company",""),
        "위치":   rec0.get("location",""),
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
    loc  = rec0.get("location","위치미정")
    code = rec0.get("site_code","")
    prefix = f"({code})" if code else ""
    fname = f"{prefix}체감온도기록관리대장_{loc}_{monday_date.year}년{monday_date.month}월{week_label_ko(wk_n)}.xlsx"

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


# ── 프론트엔드 서빙 (마지막에 마운트) ──────────────────────────────────────────
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
