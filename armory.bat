@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Запуск сервера...
echo.
start /b "" python -m uvicorn app:app
timeout /t 3 >nul
start http://127.0.0.1:8000

echo ==========================================
echo Сервер запущен!
echo Браузер открылся автоматически.
echo Если нет - перейдите по ссылке: http://127.0.0.1:8000
echo.
echo НЕ ЗАКРЫВАЙТЕ ЭТО ОКНО, ПОКА РАБОТАЕТЕ.
echo Чтобы остановить сервер, нажмите Ctrl+C.
echo ==========================================
echo.

python -m uvicorn app:app
pause