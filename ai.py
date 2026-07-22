"""Gemini – 온습도계 이미지에서 수치 추출 (모델 자동 선택)"""

import asyncio, json, logging, re, io, math
from PIL import Image, ImageOps

logger = logging.getLogger("thermohygrometer.ai")

# 사용 가능한 모델 우선순위 (무료 한도 큰 순서)
CANDIDATE_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
]

PROMPT = (
    "이것은 CAS 디지털 온습도계 사진입니다.\n"
    "\n"
    "【화면 구조】사진이 회전되어 있어도 아래 구조를 파악하여 읽으세요:\n"
    "  상단 큰 숫자 1개 = 체감온도(heat index). 절대로 temperature로 사용하지 마세요.\n"
    "  하단 작은 숫자 3개 (왼쪽부터 순서대로):\n"
    "    ① 왼쪽  : 실제 측정 온도 (°C 기호)  ← temperature는 여기서만 읽음\n"
    "    ② 가운데: 습도 (% 기호)              ← humidity는 여기서만 읽음\n"
    "    ③ 오른쪽: 시각 (HH:MM 형식)          ← time은 여기서만 읽음\n"
    "\n"
    "【소수점 주의】온도와 습도 모두 소수점 이하 숫자(.X)가 정수 부분보다 글자가 작습니다.\n"
    "  작은 글자도 반드시 정확히 읽으세요.\n"
    "  7세그먼트 LCD 혼동 주의: 0(가운데 획 없음) vs 9(가운데 획 있음),\n"
    "  5(우상단 획 없음) vs 6(우상단 획 없고 하단 닫힘) 등을 구별하세요.\n"
    "\n"
    "【출력할 값】\n"
    "1. temperature: 하단 왼쪽 ①의 실제 측정 온도(°C), 소수 1자리 포함 (예: 34.0)\n"
    "2. humidity: 하단 가운데 ②의 습도, 소수점 있으면 포함 (예: 53.8)\n"
    "3. time: 하단 오른쪽 ③의 시각(HH:MM). AM/PM이면 24시간제 변환. 없으면 null.\n"
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
            or "UNAVAILABLE" in s or "503" in s)


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
