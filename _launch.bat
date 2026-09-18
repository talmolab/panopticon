@echo off
rem Console launcher. python.exe, not pythonw.exe: pythonw has no stdout or
rem stderr, so a failure before the log file opens (a missing package, a bad
rem profile, a syntax error) would flash and vanish. The pause keeps the
rem window open on a non-zero exit so the traceback can be read.
cd /d "%~dp0"
uv run python gui.py
if errorlevel 1 pause
