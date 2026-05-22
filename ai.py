"""Gemini 2.5 Flash – 온습도계 이미지에서 수치 추출"""

import asyncio, json, re, io, math
from PIL import Image, ImageOps

PROMPT = (
    "이것은 디지털 온습도계 사진입니다.\n"
    "LCD 디스플레이에서 다음 3가지 값을 읽어주세요:\n"
    "1. temperature: 온도(°C), 소수점 포함 (예: 25.1)\n"
    "2. humidity: 습도(%) 정수 (예: 87)\n"
    "3. time: 시각, AM/PM이 있으면 24시간제로 변환 (예: '07:36'). 없으면 null\n"
    "사진이 회전되어 있어도 올바르게 읽을 것.\n"
    "JSON만 출력: {\"temperature\":25.1,\"humidity\":87,\"time\":\"07:36\"}"
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
    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash", contents=[PROMPT, img]
            )
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    else:
        raise last_err

    text = resp.text.strip()
    m = re.search(r'\{.*?\}', text, re.DOTALL)
    if not m:
        raise ValueError(f"JSON 파싱 실패: {text[:80]}")

    d  = json.loads(m.group())
    T  = float(d["temperature"]) if d.get("temperature") is not None else None
    RH = float(d["humidity"])    if d.get("humidity")    is not None else None
    t  = d.get("time")
    if t == "null": t = None

    result: dict = {"temperature": T, "humidity": RH, "time": t}

    if T is not None and RH is not None:
        hi = _heat_index(T, RH)
        result["feels_like"]  = hi
        result["heat_level"]  = _heat_label(hi)
        result["action"]      = "N/A" if hi < 31 else "추가휴식시간부여"

    return result
