@echo off
rem Console launcher. python.exe, not pythonw.exe: pythonw has no stdout or
rem stderr, so a failure before the log file opens (a missing package, a bad
rem profile, a syntax error) would flash and vanish. The pause keeps the
rem window open on any non-zero exit so the traceback can be read. The
rem string compare, not `if errorlevel 1`, because errorlevel is a signed
rem >= test that misses negative NTSTATUS codes, which is how a native
rem crash (an access violation, a Qt fail-fast) reports its exit.
cd /d "%~dp0"
uv run python gui.py
if not "%errorlevel%"=="0" pause
