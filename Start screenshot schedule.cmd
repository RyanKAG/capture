@echo off
py "%~dp0capture_email.py"
if errorlevel 1 pause
