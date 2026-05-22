@echo off
cd /d "C:\dev\2026온습도계기록어플\server"

REM Start FastAPI server
start "체감온도-서버" /min cmd /c "python main.py"

REM Wait 3 seconds for server to start
timeout /t 3 /nobreak >nul

REM Start Cloudflare tunnel (outputs URL to log file)
start "체감온도-터널" /min cmd /c "cloudflared tunnel --url http://localhost:8000 > cloudflare_url.log 2>&1"

echo 서버가 시작되었습니다.
echo 잠시 후 cloudflare_url.log 파일에서 외부 링크를 확인하세요.
