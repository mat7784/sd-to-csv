@echo off
:: build.bat — package gui.py into a single Windows executable.
::
:: Prerequisites:
::   pip install pyinstaller
::
:: Output: dist\sd_to_csv.exe
::
:: --onefile   : single self-contained .exe
:: --windowed  : suppress the black console window on Windows (GUI app)
:: --name      : output binary name

pyinstaller --onefile --windowed --name sd_to_csv gui.py
