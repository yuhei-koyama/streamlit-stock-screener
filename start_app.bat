@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed

if exist ".venv\Scripts\python.exe" goto dependencies

echo 初回起動の準備をしています。
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 -m venv .venv
if errorlevel 1 goto failed
goto dependencies

:use_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto failed

:dependencies
".venv\Scripts\python.exe" -c "import streamlit, yfinance, pandas, numpy, plotly" >nul 2>&1
if not errorlevel 1 goto launch
echo 必要なライブラリをインストールしています。インターネット接続が必要です。
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:launch
echo 検索画面を起動します。ブラウザーで http://localhost:8501 を開きます。
echo 終了するときは、このウィンドウで Ctrl+C を押してください。
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "for ($attempt = 0; $attempt -lt 30; $attempt++) { try { $response = Invoke-WebRequest -Uri 'http://localhost:8501/_stcore/health' -UseBasicParsing -TimeoutSec 2; if ($response.StatusCode -eq 200) { Start-Process 'http://localhost:8501'; exit } } catch {}; Start-Sleep -Seconds 1 }"
".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost --server.port 8501 --server.headless true --browser.serverAddress localhost
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo Python 3.14 または 3.11 をインストールしてから、もう一度起動してください。
pause
exit /b 1

:failed
echo 起動処理でエラーが発生しました。上のメッセージを確認してください。
pause
exit /b 1
