@echo off

REM set environment variable to allow DSV writes
set TI_DS_ENABLE_DSV_WRITES=1

REM set the PATH to find JRE here
set PATH=%cd%\jre\bin;%PATH%

REM set active directory to the scripts location
cd %~dp0

REM call back into the main dss.bat script
call dss.bat %*
