"""
analyze.py — report CRC health of the SD telemetry logs, per file and folder.

The question this answers: of the blocks that were ACTUALLY WRITTEN (not the
zero/stale pre-allocated tail), what fraction fail their CRC?

"Real block" detection is CRC-independent: every block the firmware writes ends
with the SD_BLOCK_MAGIC word immediately before its 12-byte trailer (the footer's
MAGIC fill always leaves at least one such word). A never-written / stale-garbage
block almost never has MAGIC exactly there. So:

    real block  := the uint32 right before the trailer == SD_BLOCK_MAGIC
    %failed CRC := (real blocks whose CRC does not verify) / (real blocks) * 100

To stay fast on huge files it probes only each block's 16-byte footer, and reads
the full 16 KB block only when that block is real (so it can CRC it).

It also flags whether the real blocks form one contiguous run from block 0 and
whether the block timestamps ever jump backwards (a sign the tail holds stale data
from an earlier, longer run).

Usage:
    py -3.12 analyze.py [ROOT ...]      # default ROOT: F:\
"""

import os
import struct
import sys
import zlib

import bin_to_csv as bc

MAGIC = 0x5D10F007
BLK = bc.SD_LOG_BLOCK_BYTES          # 16384
TRAIL = bc.SD_BLOCK_TRAILER_BYTES    # 12


def analyze_file(path):
    board, kind = bc.detect(path, None, None)
    size = bc.RECORDS[(board, kind)][1] if (board, kind) in bc.RECORDS else None

    fsize = os.path.getsize(path)
    total_blocks = fsize // BLK

    # First pass: record every WRITTEN block (MAGIC footer present), CRC-independent.
    written = {}          # idx -> (crc_ok, saved_ms, payload_bytes)
    real = 0
    real_fail = 0
    with open(path, "rb") as f:
        for idx in range(total_blocks):
            base = idx * BLK
            trailer_off = base + BLK - TRAIL
            f.seek(trailer_off - 4)
            foot = f.read(16)
            if len(foot) < 16:
                break
            if int.from_bytes(foot[0:4], "little") != MAGIC:
                continue  # not a written block (pre-allocated / stale-zero)
            saved_ms, payload_bytes, crc32 = struct.unpack("<III", foot[4:16])
            f.seek(base)
            blk = f.read(BLK)
            crc_ok = (zlib.crc32(blk[:BLK - 4]) & 0xFFFFFFFF) == crc32
            written[idx] = (crc_ok, saved_ms, payload_bytes)
            real += 1
            if not crc_ok:
                real_fail += 1

    # Current run := the contiguous prefix of written blocks from block 0, cut at
    # the first backward timestamp jump (= an older run's leftover blocks begin).
    # A magic-present-but-CRC-failed block inside the prefix is a genuine current-run
    # corruption (counted), not a run boundary; its timestamp is not trusted.
    cur_blocks = cur_fail = cur_records = 0
    prev_saved = None
    idx = 0
    while idx in written:
        crc_ok, saved_ms, payload_bytes = written[idx]
        if crc_ok:
            # A foreign record format (payload not a whole number of THIS board's
            # records) or a backward timestamp jump means we've reached an older
            # run's leftover blocks — the current run ends here.
            if size and (payload_bytes > BLK - TRAIL or payload_bytes % size != 0):
                break
            if prev_saved is not None and saved_ms < prev_saved:
                break
            prev_saved = saved_ms
            cur_records += payload_bytes // size if size else 0
        else:
            cur_fail += 1
        cur_blocks += 1
        idx += 1

    stale = real - cur_blocks                 # written blocks beyond the current run
    stale_fail = real_fail - cur_fail
    return {
        "board": board, "kind": kind, "size": size,
        "total": total_blocks, "real": real, "real_fail": real_fail,
        "cur_blocks": cur_blocks, "cur_fail": cur_fail, "cur_records": cur_records,
        "stale": stale, "stale_fail": stale_fail,
    }


def find_bins(roots):
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # skip our own csv output dirs
            dirnames[:] = [d for d in dirnames if d.lower() != "csv"]
            for name in sorted(filenames):
                if name.lower().endswith(".bin"):
                    out.append(os.path.join(dirpath, name))
    return sorted(out)


def main(argv=None):
    roots = argv if argv else [r"F:\\"]
    files = find_bins(roots)
    if not files:
        print(f"No .bin files found under: {', '.join(roots)}", file=sys.stderr)
        return 1

    hdr = (f"{'file':22} {'brd/knd':8} {'real':>6} {'rfail':>6} {'r%':>5}  | "
           f"{'curblk':>6} {'cfail':>5} {'cur%':>5} {'records':>9} {'stale':>6}")
    print(hdr)
    print("-" * len(hdr))

    agg = {"real": 0, "real_fail": 0, "cur": 0, "cur_fail": 0, "rec": 0, "stale": 0}
    for path in files:
        r = analyze_file(path)
        agg["real"] += r["real"]; agg["real_fail"] += r["real_fail"]
        agg["cur"] += r["cur_blocks"]; agg["cur_fail"] += r["cur_fail"]
        agg["rec"] += r["cur_records"]; agg["stale"] += r["stale"]
        rpct = (100.0 * r["real_fail"] / r["real"]) if r["real"] else 0.0
        cpct = (100.0 * r["cur_fail"] / r["cur_blocks"]) if r["cur_blocks"] else 0.0
        rel = os.path.relpath(path, os.path.commonpath(files)) if len(files) > 1 else path
        bk = f"{r['board'] or '?'}/{r['kind'] or '?'}"
        print(f"{rel:22} {bk:8} {r['real']:>6} {r['real_fail']:>6} {rpct:>4.1f}%  | "
              f"{r['cur_blocks']:>6} {r['cur_fail']:>5} {cpct:>4.1f}% "
              f"{r['cur_records']:>9} {r['stale']:>6}")

    print("-" * len(hdr))
    rpct = (100.0 * agg["real_fail"] / agg["real"]) if agg["real"] else 0.0
    cpct = (100.0 * agg["cur_fail"] / agg["cur"]) if agg["cur"] else 0.0
    print(f"{'TOTAL':22} {'':8} {agg['real']:>6} {agg['real_fail']:>6} {rpct:>4.1f}%  | "
          f"{agg['cur']:>6} {agg['cur_fail']:>5} {cpct:>4.1f}% {agg['rec']:>9} "
          f"{agg['stale']:>6}")
    print("\nreal  = all written blocks (MAGIC footer present), incl. leftovers from "
          "earlier runs.")
    print("r%    = CRC failures among ALL real blocks.")
    print("curblk/cur% = blocks of THIS run only (contiguous from block 0, monotonic "
          "timestamps); cur% = CRC failures among them.")
    print("stale = real blocks belonging to earlier/longer runs still on the "
          "pre-allocated sectors.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
