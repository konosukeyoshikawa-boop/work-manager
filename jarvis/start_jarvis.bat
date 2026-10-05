@echo off
REM JARVIS ローカル起動（127.0.0.1:8787・読み取り専用）
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
start "" http://127.0.0.1:8787/
python -m jarvis.server --sync
