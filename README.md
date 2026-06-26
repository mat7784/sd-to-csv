# bin-to-csv-converter

Converts the `common-protocol` SD telemetry logs (`.bin`) to CSV.

The firmware writes three logs **per board** (FCU / ECU), so up to six files:

| File           | Record type            | FCU size | ECU size |
|----------------|------------------------|----------|----------|
| `data_fast.bin`| `SystemState` (fast)   | 60 B     | 56 B     |
| `data_slow.bin`| `SystemState` (slow)   | 60 B     | 56 B     |
| `data_ext.bin` | `ExtendedSystemState`  | 132 B    | 48 B     |

## On-disk format

Each file is a stream of fixed **16384-byte blocks** (`telemetry/sd_block_footer.hpp`):

```
[ records... ][ MAGIC fill ][ trailer: saved_ms u32, payload_bytes u32, crc32 u32 ]
```

`payload_bytes` is a whole number of records. `crc32` is the zlib / reflected
CRC-32 over `[0, total-4)` — verified with Python's `zlib.crc32` (same variant).
### Pre-allocated files — where decoding stops

The firmware pre-allocates each log file to a fixed size and writes blocks from the
start. Unless the run is finalized (the `DisableLogging` flag truncates the file),
the file keeps its full size with an **unwritten tail** after the last written
block. That tail is *not always zeros* — on a reused card it can be stale data left
over from before. So the converter does **not** rely on zeros: a written block
always passes its CRC, so it decodes the contiguous valid prefix and **stops at the
first block whose CRC fails** (= end of written data). This is fast (no scanning the
whole tail) and emits no garbage rows.

Because of this, every emitted row has `crc_ok = 1` by default. The `crc_ok` column
is kept for provenance and for `--recover` mode.

`--recover`: scan the *entire* file instead of stopping; emit every CRC-valid block
and skip invalid ones. Use it to recover valid data written *after* a corrupt block
or gap. It still never emits garbage (invalid blocks are skipped, and counted in the
summary).

Note: `data_fast.bin` is only populated when the `FastRecording` flag was on for
that run. If it was off, the high-rate state was downsampled into `data_slow.bin`
instead and `data_fast.bin` stays empty → a header-only CSV. Both files hold the
**same** `SystemState` record type, just at different rates.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt    # stdlib-only; no third-party deps
```

(`requirements.txt` is intentionally empty of packages — the converter uses only
the standard library.)

## Usage

```powershell
# Convert specific files (CSV written next to each .bin)
py -3.12 bin_to_csv.py fcu_data_fast.bin ecu_data_ext.bin

# Convert every .bin in a folder, into an output dir
py -3.12 bin_to_csv.py .\logs\ -o .\csv\

# Override detection when the filename is ambiguous
py -3.12 bin_to_csv.py weird_name.bin --board fcu --kind ext

# Recover valid blocks past a corrupt block / gap (default stops at first CRC fail)
py -3.12 bin_to_csv.py ecu_data_fast.bin --recover
```

**Board / kind detection** is by filename substring: `fcu`/`ecu` for the board,
and `fast`/`slow`/`sys` (→ `SystemState`) or `ext` (→ `ExtendedSystemState`) for
the kind. Use `--board {fcu,ecu}` / `--kind {sys,ext}` to override.

## Output

One CSV per input. Values are **raw on-wire integers**:

- Enums/states (e.g. `ValveState`, `StorageState`) → their raw integer code.
- Bitfield status bytes → one integer column per named bit
  (e.g. `adc.status.initialized`, `valve0.status.open_limit_high`); the multi-bit
  `storage.status.error` field is its raw 0–3 value.
- Sensor codes (ADC counts, thermocouple/cold-junction codes, INA3221 shunt/bus
  codes) → raw signed integers, **no LSB scaling applied** (apply later for
  engineering units: TC = code·2⁻⁷ °C, CJ = code·2⁻⁶ °C, shunt = code·40 µV,
  bus = code·8 mV).
- Padding / `reserved` bytes are dropped.

Two context columns are prepended:

- `crc_ok` — `1` if the block's CRC verified, else `0`.
- `block_saved_ms` — the block trailer's saving timestamp (`saved_ms`).

Each record also carries its own `creation_timestamp_ms`.

## Performance

Pure-Python but block CRC (`zlib.crc32`) and record decoding
(`struct.iter_unpack`) run at C speed. ~440 MB converts in a couple of minutes;
progress is printed per file. A NumPy fast-path can be added later if needed.

## Staying in sync — the protocol is a LIVING DOCUMENT

The common-protocol headers are the source of truth and **will change**. The Python
decode schema in `bin_to_csv.py` is a hand-mirror of those structs, so it can drift.
`protocol_check.py` guards against that: it parses the actual `.hpp` structs/enums/
constants and re-derives each record's decode contract (ordered format char + field/
bit names), then compares it to what `bin_to_csv.py` produces. It fails loudly on a
field reorder, rename, type change, or a `reserved`→real-field swap — not just on
total-size changes.

```powershell
# Point it at your common-protocol checkout (defaults to the known path / $COMMON_PROTOCOL_DIR)
py -3.12 protocol_check.py --protocol C:\path\to\common-protocol
```

Exit code `0` = in sync, `1` = drift (prints the first mismatching column per record),
`2` = headers not found. **When it reports drift, update the matching builder function
and the `RECORDS` size in `bin_to_csv.py` to match the header, then re-run.**

## Self-test

```powershell
py -3.12 selftest.py
```

Checks every record size against the firmware `static_assert`s, round-trips a synthetic
block (including a corrupt one) through build → convert → read-back, proves the drift
guard actually fires (negative test), and runs the live header drift check (skipped if
the headers aren't present).

## GUI / exe

### Run the GUI (no build step required)

```powershell
py -3.12 gui.py
```

The window offers three input modes:

| Button | What it does |
|--------|--------------|
| **Select .bin file** | File picker filtered to `*.bin`; converts that single file. |
| **Select folder (contains .bin files)** | Folder picker; converts every `.bin` directly inside the chosen folder. |
| **Select folder (contains sub-folders with .bin files)** | Folder picker; discovers `.bin` files exactly one level deep in immediate sub-folders (e.g. `parent/run1/data_fast.bin`, `parent/run2/data_fast.bin`). Files directly in the parent are not included. |

An optional **Output folder** picker overrides the default output location (which is
the same folder as each input file). Click **Clear** to revert to the default.

Click **Convert** to start. Progress and any errors appear in the scrollable log area.
The converter runs on a background thread so the window stays responsive.

### Build the exe (Windows)

Requires PyInstaller (`pip install pyinstaller`):

```powershell
.\build.bat
```

This runs:

```
pyinstaller --onefile --windowed --name sd_to_csv gui.py
```

Output: `dist\sd_to_csv.exe` — a single self-contained executable with no console
window. Double-click to launch, or invoke from the command line.
