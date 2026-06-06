"""Gemini 2.5 Flash – 온습도계 이미지에서 수치 추출"""

import asyncio, json, logging, re, io, math
from PIL import Image, ImageOps

logger = logging.getLogger("thermohygrometer.ai")

PROMPT = (
    "이것은 디지털 온습도계(thermo-hygrometer) 사진입니다.\n"
    "LCD 디스플레이에서 다음 3가지 값을 읽어주세요:\n"
    "1. temperature: 온도(°C), 소수점 포함 (예: 25.1)\n"
    "2. humidity: 습도(%) 정수 (예: 87)\n"
    "3. time: 디스플레이 왼쪽에 표시된 시각(HH:MM 형식).\n"
    "   - 시각은 보통 디스플레이의 왼쪽 또는 상단 왼쪽에 있는 숫자입니다.\n"
    "   - 콜론(:)으로 구분된 두 숫자(시:분) 형태입니다.\n"
    "   - AM/PM이 있으면 반드시 24시간제로 변환하세요 (예: PM 1:30 → '13:30').\n"
    "   - 시각이 전혀 없으면 null.\n"
    "사진이 회전되어 있어도 올바르게 읽을 것.\n"
    "반드시 JSON만 출력: {\"temperature\":25.1,\"humidity\":87,\"time\":\"13:30\"}"
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


async def extract_from_image(image_bytes: bytes, api_key: str) -> dict:
    from google import genai as google_genai

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    client = google_genai.Client(api_key=api_key)
    _RETRIABLE = ("timeout", "timed out", "connection", "unavailable",
                  "429", "500", "503", "rate limit", "quota")
    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash-lite", contents=[PROMPT, img]
            )
            break
        except Exception as e:
            err_lower = str(e).lower()
            if any(k in err_lower for k in _RETRIABLE) and attempt < 2:
                last_err = e
                wait = min(2 ** attempt, 8)
                logger.warning("Gemini 재시도 %d/3: %s (%.0fs 후)", attempt + 1, e, wait)
                await asyncio.sleep(wait)
            else:
                logger.error("Gemini 영구 오류: %s", e)
                raise  # 영구 오류(잘못된 API 키 등)는 즉시 실패
    else:
        raise last_err

    text = resp.text.strip()
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
    t  = d.get("time")
    if t == "null": t = None

    result: dict = {"temperature": T, "humidity": RH, "time": t}

    if T is not None and RH is not None:
        hi = _heat_index(T, RH)
        result["feels_like"]  = hi
        result["heat_level"]  = _heat_label(hi)
        result["action"]      = "N/A" if hi < 31 else "추가휴식시간부여"

    return result
