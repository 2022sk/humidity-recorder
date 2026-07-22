"""Gemini – 온습도계 이미지에서 수치 추출 (모델 자동 선택)"""

import asyncio, json, logging, re, io, math
from PIL import Image, ImageOps

logger = logging.getLogger("thermohygrometer.ai")

# 사용 가능한 모델 우선순위 (무료 한도 큰 순서)
CANDIDATE_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

PROMPT = (
    "이것은 디지털 온습도계 사진입니다. 아래 두 가지 기기 유형 중 하나입니다.\n"
    "사진을 보고 어느 유형인지 판단한 뒤 해당 규칙으로 읽으세요.\n"
    "\n"
    "【유형 A: CAS 신형 — 화면에 큰 숫자 1개 + 작은 숫자 구역 3개】\n"
    "  상단 큰 숫자 1개 = 체감온도(heat index). 절대 temperature로 사용 금지.\n"
    "  하단 작은 숫자 3개 (왼→오 순서):\n"
    "    ① 왼쪽  : 실제 측정 온도 (°C)  → temperature  (소수 1자리 필수)\n"
    "    ② 가운데: 습도 (%)              → humidity      (소수 1자리 필수)\n"
    "    ③ 오른쪽: 시각 (HH:MM)          → time\n"
    "\n"
    "【유형 B: CAS 구형 — 화면이 상하 2단, TEMPERATURE/HUMIDITY/CLOCK 레이블 인쇄】\n"
    "  상단(TEMPERATURE 레이블): 온도(°C) 큰 숫자  → temperature  (소수 1자리 필수)\n"
    "  하단 왼쪽(CLOCK 레이블) : 시각 (HH:MM, AM/PM 가능) → time\n"
    "  하단 오른쪽(HUMIDITY 레이블): 습도(%)  → humidity  (표시된 그대로, 정수일 수 있음)\n"
    "\n"
    "【소수점 주의 — 두 유형 공통】\n"
    "  온도(유형 A·B 모두)와 유형 A의 습도는 소수점 이하 한 자리가 반드시 있습니다.\n"
    "  소수점 뒤 숫자는 정수 부분보다 글자 크기가 작게 표시됩니다. 작아도 정확히 읽으세요.\n"
    "  7세그먼트 혼동 주의: 0(가운데 획 없음) vs 9(가운데 획 있음).\n"
    "\n"
    "【공통】\n"
    "  사진이 회전되어 있어도 화면 구조와 레이블을 파악하여 올바르게 읽을 것.\n"
    "  AM/PM 시각은 24시간제로 변환 (PM 1:04 → 13:04). 시각 없으면 null.\n"
    "  시각은 반드시 HH:MM 형식으로 출력 (한 자리 시각도 앞에 0 붙임: 9:34 → \"09:34\").\n"
    "  콜론이 흐릿하거나 숫자가 붙어 보여도 앞 1~2자리=시, 뒤 2자리=분으로 해석할 것.\n"
    "\n"
    "반드시 JSON만 출력: {\"temperature\":34.0,\"humidity\":53.8,\"time\":\"15:25\"}"
)


def _heat_index(Ta: float, RH: float) -> float:
    Tw = (Ta * math.atan(0.151977*(RH+8.313659)**0.5)
          + math.atan(Ta+RH) - math.atan(RH-1.67633)
          + 0.00391838*RH**1.5*math.atan(0.023101*RH) - 4.686035)
    return round(-0.2442+0.55399*Tw+0.45535*Ta-0.0022*Tw**2+0.00278*Tw*Ta+3.0, 1)


def _heat_label(hi: float) -> str:
    if hi >= 38: return "위험"
    if hi >= 35: return "경고"
    if hi >= 33: return "주의"
    if hi >= 31: return "관심"
    return "-"


def _is_quota_error(e: Exception) -> bool:
    s = str(e)
    return "RESOURCE_EXHAUSTED" in s or "429" in s or "quota" in s.lower()

def _is_model_error(e: Exception) -> bool:
    s = str(e)
    return ("NOT_FOUND" in s or "404" in s or "not found" in s.lower()
            or "UNAVAILABLE" in s or "503" in s
            or "disconnected" in s.lower())


async def _try_model(client, model: str, img) -> str:
    """단일 모델로 시도, 재시도 최대 2회. quota/model 오류는 즉시 raise."""
    last_err = None
    for attempt in range(2):
        try:
            resp = client.models.generate_content(model=model, contents=[PROMPT, img])
            return resp.text.strip()
        except Exception as e:
            if _is_quota_error(e) or _is_model_error(e):
                raise  # 다음 모델로 넘길 오류
            err_lower = str(e).lower()
            retriable = any(k in err_lower for k in ("timeout", "timed out", "connection", "unavailable", "500", "503"))
            if retriable and attempt == 0:
                last_err = e
                logger.warning("Gemini[%s] 재시도: %s", model, e)
                await asyncio.sleep(2)
            else:
                raise
    raise last_err


async def extract_from_image(image_bytes: bytes, api_key: str) -> dict:
    from google import genai as google_genai

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    client = google_genai.Client(api_key=api_key)

    last_err = None
    for model in CANDIDATE_MODELS:
        try:
            text = await _try_model(client, model, img)
            logger.info("Gemini 모델 사용: %s", model)
            break
        except Exception as e:
            if _is_quota_error(e) or _is_model_error(e):
                logger.warning("Gemini[%s] 불가 → 다음 모델 시도: %s", model, str(e)[:80])
                last_err = e
                continue
            logger.error("Gemini[%s] 오류: %s", model, e)
            raise
    else:
        raise last_err  # 모든 모델 실패

    m = re.search(r'\{.*?\}', text, re.DOTALL)
    if not m:
        raise ValueError(f"JSON 파싱 실패: {text[:80]}")

    d  = json.loads(m.group())
    T  = float(d["temperature"]) if d.get("temperature") is not None else None
    RH = float(d["humidity"])    if d.get("humidity")    is not None else None
    if T is None or RH is None:
        raise ValueError("온도 또는 습도를 인식하지 못했습니다. 사진을 다시 촬영해 주세요.")
    if not (-10.0 <= T <= 60.0):
        raise ValueError(f"인식된 온도({T}°C)가 측정 범위(-10~60°C)를 벗어났습니다. 사진을 다시 촬영해 주세요.")
    if not (0.0 <= RH <= 100.0):
        raise ValueError(f"인식된 습도({RH}%)가 측정 범위(0~100%)를 벗어났습니다. 사진을 다시 촬영해 주세요.")
    t = d.get("time")
    if t == "null": t = None

    result: dict = {"temperature": T, "humidity": RH, "time": t}
    if T is not None and RH is not None:
        hi = _heat_index(T, RH)
        result["feels_like"] = hi
        result["heat_level"] = _heat_label(hi)
        result["action"]     = "N/A" if hi < 31 else "추가휴식시간부여"

    return result
