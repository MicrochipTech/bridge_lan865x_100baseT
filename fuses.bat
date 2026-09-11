@echo off
rem ===========================================================================
rem  fuses.bat - shows and programs the SAME54 fuses (NVM User Row) by name.
rem
rem  Usage:   fuses.bat                        ... board vs. release\ HEX
rem           fuses.bat --hex <file.hex>       ... board vs. another image
rem           fuses.bat --no-hex               ... board only
rem           fuses.bat --no-board             ... image only, no probe needed
rem           fuses.bat --from-bin <dump.bin>  ... a saved User Row dump
rem           fuses.bat --probe <serial>       ... pick a probe for a single run
rem           fuses.bat -help                  ... all options, with examples
rem           fuses.bat -v                     ... add every field's description
rem
rem           fuses.bat --set FIELD=VALUE      ... change fields, e.g. WDT_PER=CYC16384
rem           fuses.bat --write-from-hex       ... set all fields to the HEX's values
rem           fuses.bat --restore <backup>     ... write a saved backup back
rem           add --dry-run to see the change without writing it
rem
rem  Showing never writes and never halts the board. Programming builds the new
rem  page from the board's own (factory calibration stays), shows the change,
rem  saves a backup to json\fuse_backups\, asks, writes, reads back, and resets
rem  only if the read-back matches. flash.bat never programs the fuses - see
rem  scripts\flash_same54.py. All arguments go to scripts\fuses_same54.py.
rem ===========================================================================
setlocal

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo WARNING: no .venv in this checkout - setup.bat has not been run here.
    echo          Falling back to "python" from PATH.
    set "PY=python"
)

"%PY%" "%~dp0scripts\fuses_same54.py" %*
exit /b %errorlevel%
