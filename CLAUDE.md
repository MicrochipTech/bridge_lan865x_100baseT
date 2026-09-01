# tcpip_iperf_lan865x (net_10base_t1s_bridge) — Arbeitsanweisungen

Diese App im Fork `github.com/zabooh/net_10base_t1s_bridge.git` (`origin`, Branch `master`,
`upstream` = das öffentliche Microchip-Content-Repo `Microchip-MPLAB-Harmony/net_10base_t1s`)
wird zu einer 10BASE-T1S ↔ 100BASE-T Layer-2-Bridge auf dem ATSAME54P20A ausgebaut — analog zum
Schwesterprojekt `C:\work\t1s_bridge\bridge\t1s_100baset_bridge` (eigenes Repo, eigene
`CLAUDE.md` dort), das bereits funktioniert und als Referenz für Bridge-Konfiguration,
PHY-Zuordnung und Pin-Belegung dient.

**Alle Tools und Doku dieser Bridge-Arbeit leben hier in `apps\tcpip_iperf_lan865x\`**, nicht im
Repo-Root — der Rest von `net_10base_t1s` ist unverändertes Microchip-Content-Repo mit vielen
anderen, hier irrelevanten Apps. Neue Markdown-Dateien (Doku, Messprotokolle usw., nicht diese
`CLAUDE.md`) gehören nach **`docs\`** unter diesem Ordner.

---

## 0. Sprachregeln und Dokumentation

- **Alle Markdown-Dateien unter `docs\` sind auf Englisch zu schreiben** — unabhängig davon, in
  welcher Sprache die Session-Konsole läuft. Diese `CLAUDE.md` selbst bleibt Deutsch (Ausnahme,
  da projektinterne Arbeitsanweisung, kein an Dritte gerichtetes Dokument).
- **Jeglicher Code (C oder Python) muss vollständig auf Englisch sein** — Bezeichner, Kommentare,
  Log-/Konsolenausgaben (`SYS_CONSOLE_PRINT` usw.), Docstrings, Fehlermeldungen. Gilt für neuen
  Code und für Änderungen an bestehendem Code gleichermaßen.
- Die Session-Konsole (Chat mit dem User) läuft unabhängig davon in der Sprache, die der User
  gerade verwendet (aktuell Deutsch) — diese Regel betrifft nur Dateien, nicht die Konversation.
- **Session-Log:** Fortlaufend über die gesamte Session hinweg mitschreiben, welche Maßnahmen
  ergriffen und welche Ergebnisse/Erkenntnisse dabei erzielt wurden — Datei
  `docs\session-log.md`, chronologisch, in Englisch. Nach jedem abgeschlossenen Arbeitsschritt
  (nicht erst am Ende der Session) ergänzen, damit auch bei einem Session-Abbruch nichts verloren
  geht. Ziel: im Nachgang nachvollziehen können, mit welchen Maßnahmen was erreicht wurde.
  **Bei ausgeführten Kommandos (CLI-Tests, Builds, Skript-Aufrufe usw.) immer den exakten
  Aufruf mit sämtlichen Parametern wörtlich mitschreiben** (nicht nur zusammengefasst
  beschreiben) — auch fehlgeschlagene Versuche/Sackgassen, damit im Nachgang nachvollziehbar
  bleibt, was genau funktioniert hat und was nicht.
- **Konfigurations-Manual:** Parallel zur Session ein Dokument aufbauen, das erklärt, wie die
  Bridge konfiguriert wird (MCC-Komponenten, Pin-Belegung, Bridge-Aktivierung usw.) — Datei
  `docs\bridge-configuration-manual.md`, Englisch, für Leser gedacht, die die Bridge selbst
  nachbauen/konfigurieren wollen (anders als das Session-Log, das den Verlauf dieser Arbeit
  protokolliert).
- **Verweist der User auf „Screenshots"** (z. B. „schaue dir den Screenshot an", „kopiere die
  Screenshots rüber"), ohne einen konkreten Dateinamen/Pfad zu nennen: **immer den/die neuesten
  Screenshot(s)** in `C:\Users\M91221\OneDrive - Microchip Technology Inc\Pictures\Screenshots`
  nehmen (nach `LastWriteTime` sortiert). Diese werden nach `docs\images\` kopiert, mit einem
  **sinnvollen, beschreibenden englischen Dateinamen** (nicht dem `Screenshot YYYY-MM-DD
  HHMMSS.png`-Originalnamen) — Namensvergabe anhand des tatsächlichen Bildinhalts.

---

## 1. Harte Regel: MCC-generierter Code wird NIE von Hand angefasst

Alles unter `firmware\src\config\default\` (Treiber, `configuration.h`, `system_config.h`,
`definitions.h`, `initialization.c`, `peripheral\*\plib_*.c/.h` usw.) sowie
`firmware\tcpip_iperf_lan865x.X\tcpip_iperf_lan865x_default\` (Komponenten-YAMLs,
`mcc-config.mc4`) wird **ausschließlich über MCC + Generate Code** verändert — **niemals** durch
manuelle Edits, auch nicht als schneller Fix. Wenn im generierten Code etwas fehlt oder falsch
ist, gehört die Lösung ins MCC-GUI (Pins, Komponenten-Properties), nicht in die Datei.

**Einzige Ausnahme:** `firmware\src\app.c` / `app.h` (und andere echte User-Dateien außerhalb von
`config\default\`) — dort liegt z. B. der `TCPIP_STACK_InitCallback`-Stub (siehe Abschnitt 3).

**Vor jeder Diagnose eines Build-/Laufzeitfehlers im generierten Code:** gegen das
Schwesterprojekt diffen (`t1s_100baset_bridge`, dieselbe Hardware-Familie, nachweislich
funktionierend), bevor spekuliert wird. Mehrfach Gold wert gewesen — siehe Abschnitt 4.

---

## 2. Bauen, Flashen, Konsole

```bat
setup.bat                 :: einmalig pro Rechner, nach dem Klonen (venv, pyOCD, Debug-Fix, Makefiles)
build.bat                 :: inkrementell (Default), TYPE_IMAGE=PRODUCTION
build.bat rebuild         :: clean + full
build.bat clean
flash.bat                 :: pyOCD über EDBG-Probe
flash.bat --list          :: angeschlossene Probes
cli.bat "help"             :: Kommando über die serielle Konsole schicken
cli.bat --port COM8 --read 3 "reset"
```

- **Der User baut selbst in MPLAB X** (nicht `build.bat`) — nicht proaktiv `make`/`build.bat`
  aufrufen, um einen Fix „zu beweisen". Nur auf Zuruf bauen/flashen/testen.
- **`build.bat`/`flash.bat`/`cli.bat` liegen direkt hier**, `scripts\cli.py` und
  `scripts\flash_same54.py` darunter — Ports aus dem Schwesterprojekt
  `t1s_100baset_bridge\build.bat`/`flash.bat`/`scripts\cli.py`/`scripts\flash_same54.py`,
  Pfade relativ auf `firmware\tcpip_iperf_lan865x.X` angepasst.
- **Eigene `.venv`** (seit 2026-08-31, `setup.bat`/`batch\setup_venv.bat`,
  `scripts\requirements.txt`) — `flash.bat`/`cli.bat`/`run_gui.bat`/`run_gui_telnet.bat`/
  `run_term.bat` nutzen `%~dp0.venv\Scripts\python(w).exe`, Fallback auf globales `python`.
  Vorher zeigten diese Skripte fest auf die `.venv` des Schwesterprojekts — funktionierte,
  war aber fragil, falls das Schwesterprojekt verschoben/aufgeräumt wird. Die gesamte
  `setup.bat`/`install.bat`-Mechanik (venv, pyOCD/Probe-Auswahl über `bench.json`,
  SAME54_DFP-Debug-Fix, `genmk.bat` für headless-Makefiles) wurde 1:1 vom Schwesterprojekt
  portiert, inklusive `setup_compiler.py`/`setup_compiler.config` (seit 2026-08-31 nachgezogen,
  da auf diesem Rechner zwei XC32-Versionen installiert sind — `v4.60`, `v5.10`). **Wichtig:**
  dieser Schritt ist auch hier nur eine Notiz, kein Build-Steuerelement — `build.bat` liest
  `setup_compiler.config` nicht, genau wie im Schwesterprojekt (dort füttert der Wert nur
  `build_summary.py`s `xc32-nm`, das es hier nicht gibt). Welcher XC32 tatsächlich baut, steht
  in `nbproject\Makefile-local-default.mk` (von MPLAB X selbst geschrieben) — steuerbar nur über
  die IDE, nicht über dieses Skript.
- **`genmk.bat` (headless `nbproject\Makefile-*.mk`-Generierung) funktioniert hier,
  entgegen der älteren Notiz im projektübergreifenden MCC-Wissen** (dort stand: mehrfach
  probiert, nie funktioniert, nur „einmal in der GUI öffnen und bauen" ging — bezog sich
  aufs Schwesterprojekt, nicht auf dieses; beide Erfahrungen bleiben dokumentiert, nur an
  unterschiedlichen Projekten gemessen). Am 2026-08-31 gegen dieses Projekt getestet, mit
  einem echten Stolperer unterwegs: die vom Schwesterprojekt geportete Version hatte die
  MPLAB-X-Version fest als Liste (`v6.25 v6.20 ...`) codiert — auf diesem Rechner sind
  `v6.25` **und** `v6.35` installiert, die IDE nutzt `v6.35`, die Liste kannte nur bis
  `v6.25` und wählte damit still die falsche (ältere) Version. Ergebnis: `rc=0`, keine
  Fehlermeldung, aber `Makefile-local-default.mk` zeigte auf einen anderen DFP-Pack-Pfad
  und eine andere Java-Version als der IDE-Build — genau die Art „silently wrong", vor der
  die eigene Kopfzeile des Skripts eigentlich für `xc32-bin2hex` warnt, nur eine Ebene
  höher. **Fix:** dieselbe dynamische Verzeichnis-Erkennung wie in `build.bat`
  (`dir /b /ad /o-n "...\MPLABX\v*"`, neueste zuerst) statt der hartcodierten Liste.
  Danach wählte der Generator `v6.35`, und `Makefile-local-default.mk` kam **byte-identisch**
  zum IDE-erzeugten Original heraus; `Makefile-default.mk` unterschied sich nur noch in
  den Flag-Hash-Suffixen (harmloses Rauschen, dieselbe Art wie bei MCC-Generate-Code-Diffs,
  siehe oben) — keine Pfad-/Compiler-Abweichung mehr. Verifiziert per direktem Diff gegen
  eine vor dem Test gesicherte Kopie der IDE-erzeugten Dateien, nicht per Build (kein
  eigenmächtiger `build.bat`-Lauf, siehe Regel oben).
- **Board-COM-Port: `COM8`** (EDBG-Seriennummer `...001049`) — bestätigt am 2026-08-30.
  Weitere an diesem Tisch hängende Probes: `COM10` (`...001290`), `COM23` (`...001103`),
  gehören zu anderen Boards.
- **HEX-Ausgabe:** `firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex`
  (`TYPE_IMAGE=PRODUCTION`, nicht das `debug`-Verzeichnis).
- **`build.bat` kopiert das Hex nach jedem erfolgreichen Build zusätzlich nach
  `release\bridge_lan865x_100baseT.hex`** (seit 2026-08-31, wie im Schwesterprojekt, dort eingecheckt
  — damit ein frischer Klon flashen kann, ohne vorher zu bauen). **Nur `build.bat` aktualisiert
  diese Kopie** — ein Build direkt aus der MPLAB-X-IDE lässt `release\` veraltet stehen.
  **`flash.bat` flasht standardmäßig genau diese `release\`-Datei** (seit 2026-08-31, vorher
  `dist\`) — um stattdessen einen frischen lokalen Build zu flashen, den `dist\`-Pfad explizit
  angeben: `flash.bat firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex`.
- **`scripts\build_summary.py` (seit 2026-09-01, 1:1 vom Schwesterprojekt portiert)**
  läuft automatisch am Ende jedes `build.bat`-Laufs: Flash-/RAM-Belegung aus
  `memoryfile.xml`, Heap-/Stack-Größe aus dem `.map` (Heap gefunden, `_min_stack_size`
  in diesem Projekt **nicht** im `.map` vorhanden — Skript zeigt dafür sauber
  „-- not found in map --", kein Bug), aktive Interrupt-Handler über `xc32-nm`
  (bleibt leer, solange `setup_compiler.config` nicht existiert — einmalig
  `python scripts\setup_compiler.py` laufen lassen, exakt dieselbe Abhängigkeit wie
  im Schwesterprojekt). Archiviert zusätzlich HEX + Summary-Text zeitgestempelt unter
  `firmware\tcpip_iperf_lan865x.X\dist\default\production\image\` (gitignored, wie
  der ganze `dist\`-Baum).
- **Aus Git Bash `.bat`-Dateien mit absolutem Pfad aufrufen** (sonst „not recognized"):
  `MSYS_NO_PATHCONV=1 cmd /c "C:\work\t1s_bridge\bridge\harmony\net_10base_t1s\apps\tcpip_iperf_lan865x\flash.bat --list" < /dev/null`.
- **CLI-Antworten sind asynchron** — nach einem Kommando auf die Antwortzeile warten
  (`cli.bat --read N "..."`), nicht sofort das nächste schicken.
- **`cli.py --read N` wartet bewusst *mindestens* N Sekunden, bevor es sich beendet** (`drain()`
  hat eine feste untere Zeitschranke). Ein äußerer Bash-`timeout M`-Wrapper mit `M < N` killt den
  Prozess deshalb garantiert vorzeitig — unabhängig davon, ob das Board überhaupt geantwortet
  hätte. **2026-08-30 real passiert:** `timeout 15 ... cli.py --read 20 "reset"` lieferte
  Exit-Code 124 und wurde fälschlich als „Board hängt" gedeutet, obwohl das Board sauber gebootet
  war — derselbe Aufruf ohne `timeout`-Wrapper (oder mit `M > N`) zeigte sofort die korrekten
  Boot-Meldungen. Regel: **`cli.py` grundsätzlich ohne zusätzlichen `timeout`-Wrapper aufrufen**
  (es terminiert von selbst deterministisch nach `--read`-Sekunden); falls doch ein äußeres
  Sicherheitsnetz nötig ist, `M` mindestens `N + 15s` setzen. Vor einer „Board hängt"-Diagnose
  zusätzlich per pyOCD gegenchecken, nicht auf ein einzelnes CLI-Timeout verlassen — Rezept:
  ```
  pyocd commander -t atsame54p20a -u <probe-id> -M pre-reset --elf <production.elf> -c "reg" -c "exit"
  xc32-addr2line.exe -e <production.elf> -f -C <pc-hex> <lr-hex>
  ```
  `-M pre-reset` resettet und hält sofort an; `reg` zeigt PC/LR, `addr2line` löst sie zu
  Funktion+Zeile auf. Deutlich zuverlässiger als die serielle Konsole, um zu unterscheiden
  „Board hängt wirklich fest" (PC bleibt bei wiederholtem Aufruf/nach Wartezeit identisch) von
  „läuft normal, nur die serielle Ausgabe kam nicht an" (PC liegt irgendwo im Hauptprogramm,
  ändert sich zwischen zwei Aufrufen).
- `cli.py`s stdout-Encoding kann bei Nicht-ASCII-Bytes vom Board (z. B. Boot-Log direkt nach
  `reset`) unter Windows crashen (`UnicodeEncodeError`, cp1252-Konsole) — mit
  `PYTHONIOENCODING=utf-8` davor umgehen.

---

## 3. Bekannte MCC-Regenerate-Fallstricke (dieses Projekt, 2026-08-29/30)

- **Generate Code kann unvollständig laufen, ohne Fehlermeldung.** Mehrfach beobachtet: neue
  Treiberordner (`driver\gmac\`, `driver\ethphy\`) und Komponenten-YAMLs werden geschrieben,
  aber `configuration.h`/`system_config.h`/`initialization.c` bleiben unverändert (Mtime prüfen!).
  **Nach jedem Generate: `git status`/Mtimes der Kerndateien kontrollieren**, nicht nur den
  Build-Erfolg — ein sauberer Compile heißt nicht, dass alles neu generiert wurde.
- **`#define DRV_GMAC` fehlend → `gmac_drv_dcpt[]` wird zum Null-Element-Array** →
  `-Werror=array-bounds` beim Compile (`drv_gmac.c`, `gmac_drv_dcpt[macIndex]`). Fix: in MCC
  sicherstellen, dass die GMAC-Komponente wirklich generiert wurde (siehe oben), nicht die
  Zeile von Hand einfügen.
- **`TCPIP_STACK_NETWORK_INTERAFCE_COUNT` blieb nach dem GMAC-Hinzufügen auf `1` stehen**,
  obwohl MCCs eigene „Configuration Summary" (Overview → Config Summary) bereits korrekt
  „Network Interface: 2" zeigte — die Summary-Ansicht spiegelt nur das Modell, nicht den
  generierten Code. Erst ein tatsächlicher Generate-Lauf (Hauptfenster-Toolbar, nicht das
  TCP/IP-Configurator-Popup) schreibt es in `configuration.h`.
- **GMAC/PHY-Komponenten im Data-Link-Graphen verdrahten setzt NICHT automatisch die
  Pin-Belegung.** Die zehn RMII+MDIO-Pins mussten separat im MCC-**Pins**-Editor (eigenes
  Fenster, nicht der TCP/IP-Configurator) der GMAC-Funktion zugewiesen werden:
  `PA12, PA13, PA14, PA15, PA17, PA18, PA19, PC20, PC22, PC23`. Ohne das initialisiert die
  MDIO-Leitung nie den physischen PHY. **2026-08-30 nachgeholt** — `peripheral\port\plib_port.c`
  wurde danach 1:1 gegen die (nachweislich funktionierende) Version im Schwesterprojekt geprüft:
  alle zehn Pins jetzt identisch. **Trotzdem weiterhin derselbe Fehler beim Boot:**
  `TCP/IP Stack: GMAC MAC initialization failed` / `Initialization failed 9 - Aborting!` — die
  Pin-Belegung war also notwendig, aber offenbar nicht hinreichend. Auch Takt-Konfiguration
  (`peripheral\clock\plib_clock.c`) und alle GMAC/MIIM/PHY-Makros in `configuration.h` wurden
  gegen das Schwesterprojekt geprüft und sind inhaltlich gleich.
  **GELÖST (2026-08-30):** tatsächliche Ursache war **keine** der obigen Stellen, sondern ein zu
  klein bemessener Heap — `TCPIP_STACK_DRAM_SIZE` (MCC: `TCPIP CORE` → „Heap Configuration" →
  „TCP/IP Stack Dynamic RAM Size") stand hier auf `39250` statt der `65536`/generiert `131072` im
  Schwesterprojekt, und der Linker-`heap-size` (System → Project Configuration → XC32 Global
  Options → Linker → General → Heap Size) auf `44960` statt `163840` — beide um denselben Faktor
  (~3,6×) zu klein. Da `TCPIP_STACK_HEAP_TYPE_INTERNAL` mit `malloc_fnc = malloc` den gesamten
  TCP/IP-Heap per einem einzigen `malloc(TCPIP_STACK_DRAM_SIZE)` aus dem Linker-Heap holt, blieb
  praktisch kein Spielraum mehr für `DRV_GMAC_Initialize()`s Deskriptor-/Puffer-Allokation
  (`F_DRV_GMAC_RxCreate`/`TxCreate`, `drv_gmac.c`) — daher der Fehlschlag trotz korrekter Pins/
  Takt/PHY-Adresse. Nach Anheben beider Werte (Linker `heap-size` → `163840`,
  `TCPIP_STACK_DRAM_SIZE` → `65535`, danach Generate Code) kommen beide Interfaces sauber hoch,
  Ping auf ein anderes T1S-Node (`192.168.0.202`) erfolgreich. Ausführlicher Verlauf inkl.
  Diff-Nachweisen: `docs\session-log.md`. `drv_miim.c`/`drv_ethphy.c` (Paketversionsunterschied
  `net v3.14.5` vs. Schwester-`v3.11.1`) wurden vorsorglich per Agent Zeile für Zeile verglichen —
  keine Verhaltensunterschiede gefunden, nur MISRA-Stil. Die verwaisten `.ctu-info`-Reste von
  `drv_extphy_lan8742a.*` sind inzwischen sauber (aktueller Stand generiert konsistent nur noch
  LAN8742A-Dateien, keine LAN8740-Reste mehr, siehe Session-Log).
- **`TCPIP_STACK_DRAM_SIZE` auf `98304` (96K) angehoben — 2026-08-31, Hand-Edit,
  `configuration.h` (vorher `65535`).** Motivation: dokumentierter Nebenbefund von zuvor (siehe
  Telnet-Puffer-Eintrag oben) — der freie TCP/IP-Heap sinkt nach einer einzigen
  Telnet-Verbindung von ~17 KB auf ~3,8 KB und bleibt fragmentiert. Linker-`heap-size`
  bewusst **nicht** mit angehoben (bleibt `163840`) — Rechnung vorher geprüft: verbleibender
  Spielraum für alles andere (C-Runtime-Heap, GMAC/LAN865x-Puffer, wolfSSL) sinkt von
  `163840-65535=98305` auf `163840-98304=65536` (64K), immer noch weit über der
  dokumentierten Ausfallschwelle von ~`5710` aus dem GMAC-Init-Fehlschlag oben. Build
  erfolgreich (`BUILD SUCCESSFUL`, `release\bridge_lan865x_100baseT.hex` aktualisiert) —
  **noch nicht auf Hardware getestet.** **Muss zusätzlich im MCC-GUI gesetzt werden**
  (`TCPIP CORE` → „Heap Configuration" → „TCP/IP Stack Dynamic RAM Size" → `98304`), sonst
  fällt der Wert beim nächsten Generate Code kommentarlos auf den zuletzt im Modell
  gespeicherten Wert zurück — exakt dasselbe Muster wie beim Telnet-Puffer oben.
- **Wiederkehrender MCC-Generator-Bug: `#include <stdarg.h>` fehlt in generierten Dateien, die
  `va_start`/`va_end` nutzen** → Compile-Fehler `implicit declaration of function 'va_start'`.
  Bisher beobachtet in zwei verschiedenen generierten Dateien:
  - `drv_lan865x_api.c` (`PrintRateLimited()`) — wurde bei einem Regenerate entfernt.
  - `library\tcpip\src\telnet.c` (`F_Telnet_PRINT()`) — fehlte direkt nach Hinzufügen der
    Telnet-Server-Komponente über MCC, **2026-08-30**, gleicher Fehlerbefund.
  Kein MCC-GUI-Feld dafür vorhanden — Fix bislang durch direktes Ergänzen des Includes in der
  betroffenen generierten Datei (Ausnahme von der harten Regel oben, da reiner
  Generator-Bug ohne Konfigurationsäquivalent). **Nach jedem Generate-Lauf, der eine
  betroffene Datei anfasst, kontrollieren, ob der Include noch da ist** — MCC nimmt ihn beim
  nächsten Regenerate wieder heraus.
- **`TCPIP_STACK_InitCallback` wird von `initialization.c` als `extern` deklariert und in
  `TCPIP_STACK_Init()` verdrahtet, aber MCC generiert keine Definition** → Linkerfehler
  `undefined reference to 'TCPIP_STACK_InitCallback'`. Lösung **in `app.c`** (User-Code, siehe
  Regel 1) implementiert: liefert einen persistenten Zeiger auf eine `static
  TCPIP_STACK_INIT`-Struct mit denselben Werten, die `initialization.c` ohnehin lokal aufbaut,
  und gibt sofort `0` zurück (kein asynchrones Warten nötig).
- **Bridge aktivieren geht über eine Checkbox pro Netzwerk-Interface-Komponente**, nicht über
  eine eigene MCC-Komponente und nicht über manuelles Eintragen von
  `TCPIP_STACK_USE_MAC_BRIDGE` & Co.: im MCC-Component-Modell trägt jede
  `tcpipNetConfig_N`-Komponente (NETCONFIG-0/NETCONFIG-1 im Data-Link-Graphen) ein Boolean-Feld
  `TCPIP_NETWORK_MACBRIDGE_ADD_IDXn` — im Schwesterprojekt bei beiden Interfaces auf `true`
  gesetzt (`Add to MAC Bridge` in den Properties). Erst NETCONFIG-0 und NETCONFIG-1 dort
  aktivieren, dann Generate — MCC erzeugt daraus automatisch den
  `TCPIP_STACK_USE_MAC_BRIDGE`-Block in `configuration.h` sowie `tcpipMacbridgeTable`/
  `tcpipBridgeInitData` und den `{TCPIP_MODULE_MAC_BRIDGE, ...}`-Eintrag in `initialization.c`.
  **Seit 2026-08-30 aktiviert und als voll funktionierende End-zu-End-Bridge bestätigt**
  (Ping-Matrix, `bridge status/stats/fdb`, siehe `docs\session-log.md`).
- **`nbproject\configurations.xml`s `languageToolchainVersion`** kann vom tatsächlich beim Link
  verwendeten Compiler abweichen (bei uns `4.60` eingetragen, real gelinkt wurde mit `v5.10`,
  sichtbar am `xc32-gcc.exe`-Pfad im Build-Log) — im Zweifel den Pfad im Build-Log prüfen, nicht
  nur dieses Feld.
- **Silizium-Erratum: `OSCCTRL_DPLLSYNCBUSY.DPLLRATIO` löscht sich nie**, obwohl die DPLL korrekt
  einrastet — Microchip Silicon Errata **DS80000748K** ("SAM D5x/E5x Family Silicon Errata and
  Data Sheet Clarification"), Punkt **2.13.2 „FDPLL Ratio in DPLLnRATIO"**, betrifft beide
  Silizium-Revisionen (A und D), also auch dieses Board. Der MCC-generierte, unveränderte
  `FDPLL0_Initialize()`-Code in `peripheral\clock\plib_clock.c` wartet dort mit einer
  unbegrenzten `while(...)`-Schleife → **kompletter Boot-Hang, noch vor jeglichem App-Code**,
  reproduzierbar abhängig von scheinbar unzusammenhängenden Linker-Adressverschiebungen
  anderswo im Image (2026-08-30/31 stundenlang bisektiert, siehe `docs\session-log.md`).
  Per direktem Registerzugriff bestätigt: `DPLLRATIO` (`0x40001034`) übernimmt den Wert korrekt,
  `DPLLSTATUS` (`0x40001040`) zeigt `LOCK|CLKRDY` — nur `DPLLSYNCBUSY` (`0x4000103C`) bleibt
  fälschlich hängen. **Fix (dokumentierte Ausnahme, kein MCC-GUI-Feld dafür vorhanden):** in
  `FDPLL0_Initialize()` alle drei Wait-Schleifen (`DPLLSYNCBUSY.DPLLRATIO`, `.ENABLE`,
  `DPLLSTATUS.LOCK|CLKRDY`) mit einer Zählschranke (`CLOCK_DPLL0_SYNC_TIMEOUT = 2000`) versehen
  statt endlos zu pollen — nach jedem Generate-Lauf, der `plib_clock.c` anfasst, erneut
  anwenden. **Achtung beim Debuggen dieser Datei:** ein naiver `pyocd commander -M attach -c
  halt`-Snapshot zeigte den PC in `__dinit_clear`/C-Runtime-Startup — sowohl beim hängenden ALS
  AUCH beim bekannt guten Build (Sampling-Artefakt dieses Attach-Modus, keine echte Fundstelle).
  Verlässlich sind nur direkte Registerwerte (`DPLLSTATUS`/`DPLLRATIO`/`DPLLSYNCBUSY`) oder
  `-M pre-reset` mit PC-Vergleich über mehrere Aufrufe.
- **App-Bug (kein MCC-Thema, aber derselbe Boot-Hang verdeckte ihn):** `MIRROR_Initialize()`
  (`port_mirror.c`) alloziert sofort 8 Paketpuffer aus dem TCP/IP-Heap
  (`TCPIP_PKT_PacketAlloc()`). Wird sie — wie zunächst portiert — direkt aus
  `APP_Initialize()` aufgerufen, crasht das mit einem echten Bus-Fault (Wildpointer,
  `BFAR` außerhalb von Flash/RAM), weil `APP_Initialize()` noch synchron innerhalb von
  `SYS_Initialize()` läuft (`initialization.c`, direkt nach `TCPIP_STACK_Init()`), der TCP/IP-
  Heap zu diesem Zeitpunkt aber noch nicht zwingend fertig eingerichtet ist (`TCPIP_STACK_Init()`
  stößt die eigentliche, asynchrone Stack-Initialisierung nur an). **Fix:** `MIRROR_Initialize()`
  in `app.c`s bereits vorhandene `APP_STATE_SERVICE_TASKS`-Phase verschoben (dieselbe Stelle, an
  der schon Paket-Handler-Registrierung und `env_apply()` auf einen laufenden Stack warten).
  Die anderen drei portierten Module (`lan865x_diag`/`noip_test`/`testserver`) registrieren in
  ihrer `_Initialize()` nur CLI-Kommandos (kein Heap-Zugriff) und sind davon nicht betroffen.
- **Telnet-Login zeigte immer „Access denied" — gefixt 2026-08-31, dieselbe Bug-Klasse wie
  `MIRROR_Initialize()` oben.** `TCPIP_TELNET_AuthenticationRegister()` wurde ebenfalls aus
  `APP_Initialize()` heraus aufgerufen — die Registrierung meldete Erfolg, wurde aber von
  `TCPIP_TELNET_Initialize()` (`telnet.c`, MCC-generiert, Zeile ~317: `telnetAuthHandler =
  NULL;`) kurz danach stillschweigend überschrieben, weil dieses Modul-Init erst später als
  Teil von `TCPIP_STACK_Init()`s asynchroner Initialisierung läuft. Jeder echte Login-Versuch
  traf dadurch auf `telnetAuthHandler == NULL` und wurde ohne jeden Aufruf unseres Handlers
  abgelehnt. **Fix:** Registrierung nach `APP_STATE_SERVICE_TASKS` verschoben, direkt hinter
  `MIRROR_Initialize()`. **Verifiziert** per rohem Python-Socket-Test plus paralleler
  `tshark`-Aufnahme auf `tcp port 23`: `Logged in successfully` statt `Access denied`.
- **Telnet-Kommandos wurden nie erkannt ("Please type in a command" bei jeder Zeile) —
  gefixt 2026-08-31.** TeraTerm sendet jedes Zeichen einzeln und Enter als **`0d 00`
  (CR NUL)**, nicht CR LF (per `tshark`-Mitschnitt auf `tcp port 23` bestätigt, RFC 854
  erlaubt beides). `sys_command.c`s (MCC-generierter) Zeicheneditor (`RunCmdTask()`)
  kennt nur `\r`/`\n` als Zeilenende — das nachfolgende NUL-Byte fiel in den
  generischen "Zeichen einfügen"-Zweig und landete als führendes Byte im NÄCHSTEN
  Kommandopuffer. `strncpy()`/String-Funktionen sehen einen mit `\0` beginnenden
  String als leer an, selbst wenn danach echter Text folgt — deshalb funktioniert das
  jeweils erste Kommando einer Sitzung, jedes weitere schlägt fehl. **Fix
  (dokumentierte Ausnahme, `sys_command.c`):** neuer `else if (newCh == '\0')`-Zweig
  direkt nach der `\r`/`\n`-Behandlung, der das Byte einfach verwirft.
  `patches/sys_command.patch` neu erzeugt (Tool deckt jetzt 5 Dateien ab).
  **Verifiziert:** zwei aufeinanderfolgende zeichenweise `"help"`-Eingaben über einen
  rohen Socket sowie live in TeraTerm — beide funktionieren jetzt zuverlässig.
- **Alle eigenen Kommandos antworteten über Telnet ins Leere — gefixt 2026-08-31.**
  Direkte Folge des Login-Fixes: Kommandos wurden jetzt geparst, aber ihre Ausgabe
  landete immer auf der seriellen Konsole, nie im Telnet-Client. Ursache: alle sechs
  eigenen Modul-Dateien (`env.c`, `app.c`, `port_mirror.c`, `lan865x_diag.c`,
  `noip_test.c`, `testserver.c`) benutzten `SYS_CONSOLE_PRINT()` (fest auf
  `SYS_CONSOLE_DEFAULT_INSTANCE`, d.h. immer seriell) statt des `pCmdIO`, den jeder
  `SYS_CMD_FNC`-Handler bekommt — 231 Fundstellen. **Fix:** neuer Header
  `firmware/src/cmd_print.h` mit `CMD_PRINT(pCmdIO, ...)`/`CMD_MSG(pCmdIO, str)`
  (Wrapper um `pCmdIO->pCmdApi->print/msg`), alle Kommando-Antworten in allen sechs
  Dateien umgestellt; Boot-/Hintergrund-Logs (kein Kommando-Kontext, z. B.
  `APP_Tasks()`s Paket-Log-Drain, `TESTSERVER_Tasks()`s Connect/Disconnect-Meldungen)
  bewusst unverändert auf `SYS_CONSOLE_PRINT` gelassen. Für `lan865x_diag.c`s
  asynchrone Register-Operationen (Ergebnis kommt erst später aus
  `LAN865X_DIAG_Tasks()`, nach Rückkehr des Kommando-Handlers) zusätzlich
  `CMD_PRINT_OR_CONSOLE(pCmdIO, ...)` plus ein gemerktes `s_diag_pCmdIO` (sicher, weil
  das Modul ohnehin nur eine Operation gleichzeitig zulässt, `LAN865X_DIAG_Busy()`).
  **Verifiziert** über echten Telnet-Socket: `showenv`/`stats`/`meminfo`/`mirror`/
  `lanhelp` UND die beiden asynchronen Fälle `lan_read`/`plca_stat` (inkl. verketteter
  RMW+Multi-Step-Read-Sequenz) liefern jetzt korrekt über Telnet.
- **Telnet-Ausgabepuffer zu klein für größere Kommando-Ausgaben (z. B. `dump`/`netinfo`)
  — 2026-08-31, echtes MCC-Konfigurationsfeld, kein Hand-Patch.**
  `TCPIP_TELNET_SKT_TX_BUFF_SIZE` stand auf `0` (= Framework-Default), spürbar zu klein
  (`F_Telnet_MSG()` in `telnet.c` verwirft den Rückgabewert von `NET_PRES_SocketWrite()`
  — was nicht in den Puffer passt, geht kommentarlos verloren). Testreihe mit
  `dump <addr> <größe>` (Größe frei wählbar) plus `netinfo` über echten Telnet-Socket:
  0 schneidet schon bei 200 Byte ab; 2048 deckt 200, nicht 500/800; **3072 deckt 200 und
  500 vollständig** (nicht 800); 4096 deckt alle drei, drückt den größten freien
  TCP/IP-Heap-Block nach einem Connect/Dump/Disconnect-Zyklus aber auf nur ~720 Byte —
  angesichts früherer Heap-Erschöpfungs-Bugs in diesem Projekt zu knapp. **Auf 3072
  gesetzt** als Mittelweg (aktuell Hand-Edit in `configuration.h` für die Testreihe —
  muss vor dem nächsten Generate Code auch über MCCs Telnet-Server-Komponente
  gesetzt werden, sonst fällt es beim Regenerieren still auf 0 zurück). Nebenbefund,
  unabhängig von der Puffergröße: der freie TCP/IP-Heap sinkt nach einer einzigen
  Telnet-Verbindung von ~17 KB (frischer Boot) auf ~3,8 KB und bleibt fragmentiert
  (größter Block nur ~1,6 KB) — noch nicht weiter untersucht.
- **`dump` bei größeren Byte-Zahlen (z. B. 500) lieferte kaputte/korrumpierte Ausgabe —
  Eigenverschulden aus dem obigen Fix, behoben 2026-08-31.** Beim Aufspalten von
  `DumpMem()` in eine `pCmdIO`-fähige `CmdDumpMem()` (für den `dump`-Befehl) ging der
  Busy-Wait-Schutz des Originals verloren (`SYS_CONSOLE_WriteFreeBufferCountGet()`,
  seriell-spezifisch — für Telnet gibt es dafür keine Entsprechung über `pCmdIO`).
  `CmdDumpMem()` druckte Zeilen ungebremst in CPU-Geschwindigkeit, überholte damit
  sowohl den seriellen 1024-Byte-Ringpuffer (`SERCOM1_USART_Write()` verwirft still,
  was nicht passt) als auch Telnets `F_Telnet_MSG()` (verwirft `NET_PRES_SocketWrite()`s
  Rückgabewert ebenso) — Resultat: nicht nur Abschnitt, sondern **korrumpierte,
  ineinander verschachtelte Bytes** mitten in der Ausgabe.
  **Erster Fix (verworfen):** feste 10ms-Pacing-Pause nach jeder Zeile — funktionierte,
  bremste aber JEDEN Dump unnötig, auch viel zu kleine. **Besserer Fix (Nutzer-Idee:
  „das Backpressure hatte im Schwesterprojekt schon funktioniert"):** die ORIGINALE
  `SYS_CONSOLE_WriteFreeBufferCountGet()`-Busy-Wait aus `DumpMem()` unbedingt
  wiederverwendet, ganz ohne Geräteerkennung — der Trick: diese Prüfung hängt nur vom
  seriellen Ringpuffer ab, der bei einem Telnet-ausgelösten Dump praktisch immer frei
  ist (nichts anderes schreibt gleichzeitig seriell), meldet dort also fast sofort
  „genug Platz" und bremst faktisch nicht; bei einem seriell ausgelösten Dump greift
  exakt dieselbe präzise, lastadaptive Drosselung wie zuvor. `app_wait_ms()` wieder
  entfernt. **Verifiziert:** identische Ergebnisse wie mit der festen Pause (seriell
  `dump 800` komplett und sauber; Telnet `dump 500` komplett, `dump 800` sauber an der
  3072-Byte-Puffergrenze abgeschnitten, `netinfo` komplett) — jetzt ohne künstliche
  Verzögerung bei kleinen/Telnet-Dumps.
- **Echtes Telnet-Backpressure in `F_Telnet_MSG()` — gefixt 2026-08-31.** Der obige
  Fix umgeht das Puffer-Limit nur, deckt es nicht ab: Ausgaben über
  `TCPIP_TELNET_SKT_TX_BUFF_SIZE` (3072 Byte) hinweg blieben abgeschnitten.
  Erster Versuch — ein begrenztes Busy-Wait auf `NET_PRES_SocketWriteIsReady()`,
  auch mit zusätzlichem `NET_PRES_SocketFlush()` — brachte **nichts** (`dump 800`
  weiterhin nur ~3080 von ~4011 Byte, dafür 6,6s statt praktisch sofort). Ursache:
  in diesem Bare-Metal-Single-Superloop-Aufbau läuft `SYS_CMD_Tasks()` — das ruft
  den Kommando-Handler und darüber `F_Telnet_MSG()` — in `SYS_Tasks()`
  (`config/default/tasks.c`) **vor** `TCPIP_STACK_Task()`/`NET_PRES_Tasks()`.
  Nichts leert den Telnet-Sende-Puffer, solange diese beiden nicht laufen — anders
  als beim UART, wo eine Hardware-Interrupt unabhängig vom Hauptloop weiterläuft.
  Nutzerfrage „wäre es möglich, während des Wartens `SYS_Tasks()` aufzurufen?" —
  Antwort: nicht die ganze Funktion (würde rekursiv in `SYS_CMD_Tasks()` selbst
  — den gerade aktiven Stackframe mit eigenem statischem Parser-Zustand — und in
  `APP_Tasks()` hineinlaufen), aber genau die zwei relevanten Aufrufe sind aus
  `F_Telnet_MSG()` heraus nie reentrant (diese Funktion wird ausschließlich über
  die `SYS_CMD_API` `.msg`/`.print`-Callbacks erreicht, also nur aus
  `SYS_CMD_Tasks()`, einem Geschwister von `TCPIP_STACK_Task()` in `SYS_Tasks()`,
  nie darin verschachtelt). Neue `APP_PumpNetworkStack()` (`app.c`/`app.h`) kapselt
  `TCPIP_STACK_Task(sysObj.tcpip)` + `NET_PRES_Tasks(sysObj.netPres)`;
  `F_Telnet_MSG()`s Busy-Wait ruft sie statt nur zu spinnen. **Verifiziert:**
  `dump 800/2000/4000/8000` alle vollständig (4011/9938/19813/39554 Byte) in
  konstant ~1,8–2,2s statt Abbruch bei ~3072 Byte; `netinfo` ebenfalls vollständig.
  Neuer Hand-Patch `patches/telnet.patch`, Details:
  `docs/mcc-generated-code-patches.md` Punkt 8.
- **Nachtrag: `F_Telnet_MSG()` korrumpierte große Dumps weiterhin intermittierend
  — gefixt 2026-08-31.** Nutzer-Test mit `dump 0x20000000 32000` zeigte:
  gelegentlich (nicht bei jedem Lauf — ein Timing-Race, keine feste
  Größengrenze) fehlte das Ende einer Zeile (z. B. nur 9 statt 16 ASCII-Punkte)
  und die **nächste** Zeilenadresse hing direkt ohne `\n\r` dran —
  Byte-Gesamtzahl variierte bei jedem Lauf (158020/158029/157993/158065/158212).
  Auf Nutzerwunsch per `tshark`-Mitschnitt (`follow,tcp,raw`) gegengeprüft: der
  Draht zeigt denselben Inhalt wie der Python-Client, kein Client-Artefakt —
  dieser eine Mitschnitt-Lauf lief zufällig sauber durch, was zur
  Race-Condition-These passt (ein deterministischer Fehler wäre jedes Mal
  gleich aufgetreten). Ursache: `NET_PRES_SocketWriteIsReady()`s Vorab-Prüfung
  sagt nicht zuverlässig voraus, was ein einzelner `NET_PRES_SocketWrite()`-
  Aufruf tatsächlich annimmt — der Rückgabewert wurde trotzdem verworfen,
  dieselbe „fire and forget"-Bugklasse wie beim seriellen
  `SERCOM1_USART_Write()`, nur intermittierend statt konsistent. Gefixt durch
  Schleife über den echten Rückgabewert: Rest erneut schreiben, dazwischen
  `APP_PumpNetworkStack()`, begrenzt durchs bestehende 500ms-Timeout.
  Nutzerfrage, ob `CmdDumpMem()`s eigener serieller Busy-Wait
  (`SYS_CONSOLE_WriteFreeBufferCountGet(...) < pos`) jetzt „auch für UART und
  Telnet funktionieren sollte" — Antwort: tut er bereits, aus zwei
  verschiedenen Gründen (serielle Drossel bei UART, harmloses Sofort-Weiter
  bei Telnet, da die echte Telnet-Korrektheit jetzt komplett in
  `F_Telnet_MSG()` liegt) — der zugehörige Kommentar in `app.c` war veraltet
  (behauptete fälschlich, Korrektheit käme von der Puffergröße
  `TCPIP_TELNET_SKT_TX_BUFF_SIZE`) und wurde korrigiert. **Verifiziert:**
  `dump 0x20000000 32000` 5× hintereinander, alle 5 Läufe exakt 158065 Byte,
  keine verklebten Zeilen mehr — vorher bei jedem Lauf unterschiedlich.
- **LAN865x-RX-Pfad hatte eine echte Race Condition — gefixt 2026-08-31 (siehe
  `docs/session-log.md` für die volle Herleitung).** Ursprünglicher Befund:
  `rxPkt->pDSeg->segLen` wich vom im IP-Header deklarierten Gesamtlängenwert ab, und zwar
  **nicht-deterministisch** — dieselbe periodische Nachricht zeigte zu unterschiedlichen
  Zeitpunkten `88` und `102` Byte (wahre Länge: 98). Root Cause per derselben Methodik wie im
  Schwesterprojekt gefunden (`FALLSTRICKE.md`, GMAC-RX-Race, 2026-08-27): `_Lock()`/`_Unlock()`
  in `drv_lan865x_api.c` wickeln nur `OSAL_MUTEX_Lock/Unlock` ein, was auf diesem Bare-Metal-Build
  (`osal_impl_basic.h`) **nur ein einfaches Flag ist, keine Interrupts sperrt** — der
  SPI-Transfer-Complete-Callback `_EventHandlerSPI()` → `TC6_SpiBufferDone()` (läuft aus einem
  echten Hardware-Interrupt) kann dadurch jederzeit mitten in die Task-Kontext-Verarbeitung von
  `TC6_Service()`/`process_rx()` hineinfeuern — exakt dieselbe Fehlerklasse (task-lokales „Lock",
  das die tatsächlich konkurrierende ISR nicht blockiert).
  **Fix (dokumentierte Ausnahme, `DRV_LAN865X_INSTANCES_NUMBER==1` in diesem Projekt macht eine
  einzelne gespeicherte Interrupt-State-Variable sicher):** `_Lock()`/`_Unlock()` rahmen jetzt
  zusätzlich `SYS_INT_Disable()`/`SYS_INT_Restore()` — ein echter kritischer Abschnitt, wie im
  Schwesterprojekt.
  **Verifiziert:** nach dem Fix zeigte dieselbe Testnachricht über mehrere Stichproben hinweg
  konstant `102` (keine Streuung mehr); der volle `sniffer_capture_test.py`-Vollständigkeitstest
  zeigte für **alle 3982** iperf-UDP-Frames exakt dieselbe `frame.len/ip.len/udp.length`-Kombination
  (0 Varianz) — die nicht-deterministische Komponente ist nachweislich behoben.
  **Der damals offen gelassene Rest (fester, größenabhängiger Offset: ~1512-Byte-Frames
  10 Byte zu wenig, ~98-Byte-Frames 4 Byte zu viel) ist inzwischen root-gecauset und
  gefixt — 2026-08-31, siehe `docs/session-log.md` für die volle Herleitung.** Zwei
  unabhängige, sich addierende Effekte, nicht eine einzelne Chunk-Grenzen-Eigenheit:
  1) `TC6_CB_OnRxEthernetPacket()` meldet `len`/`segLen` durchgehend 4 Byte zu groß
     (vermutlich die vom T1S-PHY noch mitgelieferte, nie abgeschnittene 4-Byte-FCS,
     entgegen `tcpip_mac.h`s dokumentiertem RX-Vertrag). An dieser Stelle stimmen
     `len` und `segLen` aber immer überein — kein Rennen, keine Korruption.
  2) Der generische, MCC-generierte Stack-Code (`library/tcpip/src/tcpip_manager.c`,
     Zeile ~2544) zieht davon `sizeof(TCPIP_MAC_ETHERNET_HEADER)` (14) ab, bevor er an
     registrierte Paket-Handler wie `pktEth0Handler()`/`MIRROR_Eth0Rx()` weiterreicht —
     dokumentiertes, korrektes Standardverhalten des Frameworks, kein Bug für sich.
  **Der eigentliche Bug (nicht MCC-generiert, App-Code):** `port_mirror.c`s
  `MIRROR_Eth0Rx()` benutzte `rxPkt->pDSeg->segLen` an dieser Stelle direkt als volle
  Kopierlänge ab `pMacLayer` — es bedeutet dort aber "Payload nach dem 14-Byte
  MAC-Header", nicht "volle Framelänge". Jeder gesniffte/gespiegelte RX-Frame wurde
  dadurch 14 Byte zu kurz kopiert (bei kleinen Frames unsichtbar, solange
  `MIRROR_SAFE_FRAME_LEN`s Clamp nicht griff; bei allem über einem TC6-SPI-Chunk sehr
  sichtbar). **Fix (eine Zeile):** `rxPkt->pDSeg->segLen + sizeof(TCPIP_MAC_ETHERNET_HEADER)`
  als Framelänge an `mirror_ethpkt_to_eth1()` übergeben — bewusst nur an der RX-Stelle,
  nicht bei `mirror_eth0_tx_hook()` (TX-Pakete durchlaufen den RX-seitigen
  Header-Abzug nie, ihr `segLen` bedeutet dort schon "volle Framelänge").
  **Hatte doch Funktionsschaden, anders als hier ursprünglich vermerkt:** korrupte
  Sniffer-Captures (jedes große Frame zeigte in Wireshark "Previous segment not
  captured"/"ACKed unseen segment") — die normale Bridge-Weiterleitung
  (`tcpip_mac_bridge.c`) läuft nie durch `MIRROR_Eth0Rx()` und war nie betroffen.
  **Verifiziert** (zweimal: direkt nach dem Fix und nochmal nach vollständigem Entfernen
  der Diagnose-Instrumentierung + Clean-Rebuild): `sniffer_capture_test.py` zeigt
  UDP/TCP in beiden Richtungen `COMPLETE`, keine „shorter than IP/UDP header claims"-
  Warnung mehr; `tshark` bestätigt `frame.len=1514`/`tcp.len=1460` (vorher
  `1504`/`1450`) und null `tcp.analysis.lost_segment`-Treffer.
  Die temporäre Diagnose-Instrumentierung (`g_tc6DiagEnable` in `tc6.c`/
  `drv_lan865x_api.c`, plus das dafür ergänzte `MIRRORDIAG` in `port_mirror.c`) ist
  wieder vollständig entfernt.
- **`suppressTx` aus dem Schwesterprojekt portiert (2026-08-31):** `setenv sniffer 1` +
  `saveenv` sorgte vorher nur für das RAM-Flag „sniffer ON at boot", ohne den T1S-Sender
  wirklich stummzuschalten — bestätigt per `lan_read 0x000308F9` (`T1SPMACTL`), das direkt
  nach dem Boot noch `0x0` zeigte statt `0x4000` (TXD). Grund: das Schwesterprojekt hat dafür
  ein eigenes `suppressTx`-Feld in `DRV_LAN865X_Configuration`, das MCC hier nicht generiert.
  **Fix (dokumentierte Ausnahme, drei Hand-Patches):** `bool suppressTx;` in `drv_lan865x.h`
  ergänzt (gleiche Position wie im Schwesterprojekt, nach `rxCutThrough`); in
  `drv_lan865x_api.c`s `_InitUserSettings()`-Zustandsautomat einen neuen `case 9` eingefügt,
  der `T1SPMACTL=0x4000` schreibt, wenn `drvCfg.suppressTx` gesetzt ist — **vor** dem
  abschließenden `NETWORK_CONTROL`/TXEN-Write (der dafür von `case 9` auf `case 10`
  hochgezählt wurde); in `initialization.c` `.suppressTx = false,` im Default-Initialisierer
  und `drvLan865xInitData[0].suppressTx = env_sniffer();` direkt neben der bestehenden
  `nodeId`/`nodeCount`-Übernahme ergänzt. **Verifiziert:** `lan_read 0x000308F9` zeigt jetzt
  sofort nach dem Boot `0x00004000`, noch bevor irgendein `sniffer`-Kommando lief. Board nach
  dem Test wieder auf `sniffer OFF` zurückgesetzt (register-bestätigt).

---

## 4. Schwesterprojekt als Referenz

`C:\work\t1s_bridge\bridge\t1s_100baset_bridge` — eigenes Git-Repo, eigene `CLAUDE.md` dort.
Gleiche Hardware-Familie (SAM E54 + LAN865x per SPI + 100BASE-T-PHY per GMAC/RMII), bereits
verifiziert lauffähig, dort **LAN8740A** auf einem `LAN8740A PHY Daughter Board (AC320004-3)`.
Bei Unklarheiten zu Bridge-Konfiguration, GMAC/PHY-Init-Daten oder Pin-Belegung: die
entsprechende generierte Datei (`initialization.c`, `configuration.h`,
`peripheral\port\plib_port.c`, Komponenten-YAMLs unter
`firmware\T1S_100BaseT_Bridge.X\T1S_100BaseT_Bridge_default\components\`) 1:1 dagegen diffen,
bevor spekuliert wird — mehrfach der schnellste Weg zur echten Ursache.

---

## 5. Erkenntnisse festhalten

`C:\work` ist Wegwerf-Arbeitsbereich, Auto-Memory hängt am Pfad/Repo — Dauerhaftes deshalb hier
in Abschnitt 3 ablegen (datiert, `YYYY-MM-DD — Fehler/Erkenntnis → Lösung`, ein bis zwei Sätze),
nicht nur im Memory. Zieldatei vorher lesen, um Duplikate zu vermeiden. Besonders festhalten:
Fehler samt richtiger Lösung, und Sackgassen („Weg A geht nicht, weil … → nicht nochmal
versuchen"). Andere Markdown-Dokus (Messprotokolle, Vertiefungen) gehören nach `docs\`, nicht in
diese Datei.
