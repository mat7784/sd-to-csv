"""throughput.py — real record rate (records/second) of the current run per file.

Reuses analyze.py's current-run detection (contiguous MAGIC blocks from block 0,
monotonic saved_ms). Duration is taken from the record-level creation_timestamp_ms
(first 4 bytes of every record) of the very first record to the very last record
of the run. Rate = records / (span_seconds).
"""
import os
import struct
import sys
import zlib

import bin_to_csv as bc

MAGIC = 0x5D10F007
BLK = bc.SD_LOG_BLOCK_BYTES
TRAIL = bc.SD_BLOCK_TRAILER_BYTES


def run_throughput(path):
    board, kind = bc.detect(path, None, None)
    size = bc.RECORDS[(board, kind)][1] if (board, kind) in bc.RECORDS else None
    fsize = os.path.getsize(path)
    total_blocks = fsize // BLK

    records = 0
    first_ts = None
    last_ts = None
    prev_saved = None
    first_saved = None
    last_saved = None

    with open(path, "rb") as f:
        idx = 0
        while idx < total_blocks:
            base = idx * BLK
            trailer_off = base + BLK - TRAIL
            f.seek(trailer_off - 4)
            foot = f.read(16)
            if len(foot) < 16:
                break
            if int.from_bytes(foot[0:4], "little") != MAGIC:
                break  # current run is contiguous; gap ends it
            saved_ms, payload_bytes, crc32 = struct.unpack("<III", foot[4:16])
            f.seek(base)
            blk = f.read(BLK)
            crc_ok = (zlib.crc32(blk[:BLK - 4]) & 0xFFFFFFFF) == crc32
            if crc_ok:
                if size and (payload_bytes > BLK - TRAIL or payload_bytes % size != 0):
                    break
                if prev_saved is not None and saved_ms < prev_saved:
                    break
                prev_saved = saved_ms
                nrec = payload_bytes // size if size else 0
                if nrec:
                    if first_ts is None:
                        first_ts = struct.unpack_from("<I", blk, 0)[0]
                        first_saved = saved_ms
                    last_ts = struct.unpack_from("<I", blk, (nrec - 1) * size)[0]
                    last_saved = saved_ms
                records += nrec
            # crc-failed block inside the run is current-run corruption: count nothing
            idx += 1

    span_ms = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else 0
    saved_span_ms = (last_saved - first_saved) if first_saved is not None else 0
    return {
        "board": board, "kind": kind, "records": records,
        "span_ms": span_ms, "saved_span_ms": saved_span_ms,
        "first_ts": first_ts, "last_ts": last_ts,
    }


def main(argv):
    roots = argv if argv else [r"F:\\"]
    files = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d.lower() != "csv"]
            for name in sorted(fn):
                if name.lower().endswith(".bin"):
                    files.append(os.path.join(dp, name))
    files = sorted(files)

    hdr = f"{'file':22} {'brd/knd':8} {'records':>9} {'span_s':>10} {'rec/s':>10}"
    print(hdr)
    print("-" * len(hdr))
    for path in files:
        r = run_throughput(path)
        span_s = r["span_ms"] / 1000.0
        rate = r["records"] / span_s if span_s > 0 else 0.0
        bk = f"{r['board'] or '?'}/{r['kind'] or '?'}"
        rel = os.path.basename(path)
        print(f"{rel:22} {bk:8} {r['records']:>9} {span_s:>10.3f} {rate:>10.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
