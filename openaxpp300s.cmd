@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -X utf8 -m openaxpp300s %*
