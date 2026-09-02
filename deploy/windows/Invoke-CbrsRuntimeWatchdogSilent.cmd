@echo off
rem Task Scheduler ignores a duplicate start when either long-running task is
rem already active. Redirect all output so this watchdog remains fully silent.
schtasks.exe /run /tn "CBRS User Dashboard" >nul 2>&1
schtasks.exe /run /tn "CBRS User Worker" >nul 2>&1
exit /b 0
