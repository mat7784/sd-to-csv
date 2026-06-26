"""Self-test: verify record sizes against the firmware static_asserts and
round-trip a synthetic block (build -> convert -> read back)."""
import csv
import os
import struct
import tempfile
import zlib

import bin_to_csv as b
import protocol_check as pc


def check_sizes():
    print("== record sizes ==")
    ok = True
    for (board, kind), (builder, exp) in b.RECORDS.items():
        fmt, header, instr, size = b.compile_record(builder())
        good = size == exp
        ok = ok and good
        print(f"  {board}/{kind}: size={size} expected={exp} "
              f"cols={len(header)} {'OK' if good else 'MISMATCH'}")
    assert ok, "record size mismatch"


def build_block(record_bytes_list, block_size=b.SD_LOG_BLOCK_BYTES, corrupt=False):
    """Assemble one on-disk block like stampSdBlockFooter()."""
    payload = b"".join(record_bytes_list)
    payload_bytes = len(payload)
    total = block_size
    block = bytearray(total)
    block[:payload_bytes] = payload
    trailer_off = total - b.SD_BLOCK_TRAILER_BYTES
    # MAGIC fill, phase-aligned to the block offset
    MAGIC = 0x5D10F007
    for i in range(payload_bytes, trailer_off):
        block[i] = (MAGIC >> (8 * (i & 3))) & 0xFF
    saved_ms = 123456
    struct.pack_into("<II", block, trailer_off, saved_ms, payload_bytes)
    crc = zlib.crc32(bytes(block[:total - 4])) & 0xFFFFFFFF
    if corrupt:
        crc ^= 0x1
    struct.pack_into("<I", block, total - 4, crc)
    return bytes(block), saved_ms


def round_trip():
    print("== round-trip (fcu/ext) ==")
    builder, exp = b.RECORDS[("fcu", "ext")]
    fmt, header, instr, size = b.compile_record(builder())

    # Two records: fill bytes with a known ramp so we can spot-check a field.
    rec0 = bytes((i * 7) & 0xFF for i in range(size))
    rec1 = bytes((255 - i) & 0xFF for i in range(size))
    # Force a recognizable creation_timestamp_ms in rec0 (first 4 bytes LE).
    rec0 = struct.pack("<I", 0xDEADBEEF) + rec0[4:]

    good0, saved_ms = build_block([rec0, rec1])
    bad, _ = build_block([rec0], corrupt=True)        # CRC fails (zero-fill tail)
    good1, _ = build_block([rec1])                    # valid block AFTER the gap
    # A CRC-VALID block whose payload is foreign (not a whole number of 132-byte
    # records) — e.g. stale ECU data left on the pre-allocated sectors.
    foreign = bytearray(build_block([rec1])[0])
    struct.pack_into("<I", foreign, b.SD_LOG_BLOCK_BYTES - 8, 130)  # payload_bytes=130
    import zlib as _z
    crc = _z.crc32(bytes(foreign[:b.SD_LOG_BLOCK_BYTES - 4])) & 0xFFFFFFFF
    struct.pack_into("<I", foreign, b.SD_LOG_BLOCK_BYTES - 4, crc)  # re-stamp CRC

    d = tempfile.mkdtemp()
    binp = os.path.join(d, "fcu_data_ext.bin")
    with open(binp, "wb") as f:
        f.write(good0)            # block 0: valid, 2 records
        f.write(bytes(foreign))   # block 1: CRC-OK but foreign format -> stop here
        f.write(good1)            # block 2: valid, only reachable in --recover

    # Default: stop at the foreign-format block (other board's / older records).
    csvp = os.path.join(d, "fcu_data_ext.csv")
    board, kind = b.detect(binp, None, None)
    assert (board, kind) == ("fcu", "ext"), (board, kind)
    blocks, records, skipped, stopped_at, reason = b.convert_file(binp, csvp, board, kind)
    print(f"  default : blocks={blocks} records={records} skipped={skipped} "
          f"stopped_at={stopped_at} reason={reason}")
    assert blocks == 1 and records == 2 and skipped == 0
    assert stopped_at == 1 and reason == "foreign-format"

    with open(csvp, newline="") as f:
        rows = list(csv.reader(f))
    hdr = rows[0]
    assert hdr[:3] == ["crc_ok", "block_saved_ms", "creation_timestamp_ms"]
    assert len(rows) == 1 + 2  # header + 2 records, nothing past the foreign block
    r = dict(zip(hdr, rows[1]))
    assert r["crc_ok"] == "1"
    assert r["block_saved_ms"] == str(saved_ms)
    assert r["creation_timestamp_ms"] == str(0xDEADBEEF)

    # Recover: skip the foreign block, still pick up the valid block after it,
    # and NEVER decode the foreign (other-board) block.
    csvp2 = os.path.join(d, "fcu_data_ext_recover.csv")
    blocks, records, skipped, stopped_at, reason = b.convert_file(
        binp, csvp2, board, kind, recover=True)
    print(f"  recover : blocks={blocks} records={records} skipped={skipped} "
          f"stopped_at={stopped_at}")
    assert blocks == 2 and records == 3 and skipped == 1 and stopped_at is None

    # Also confirm a plain CRC failure stops with reason 'crc'.
    binp2 = os.path.join(d, "fcu_data_ext_crc.bin")
    with open(binp2, "wb") as f:
        f.write(good0)
        f.write(bad)
    _, _, _, sa, reason = b.convert_file(binp2, os.path.join(d, "x.csv"), board, kind)
    assert sa == 1 and reason == "crc", (sa, reason)
    print(f"  header cols={len(hdr)} first few: {hdr[2:8]}")
    print("  OK")


def drift_guard_negative():
    """Prove the drift guard actually fires: alter a token and expect a diff."""
    print("== drift guard (negative) ==")
    good = pc.python_tokens("fcu", "ext")
    # 1. a renamed field
    renamed = list(good)
    renamed[3] = ("S", renamed[3][1], "RENAMED")
    assert pc._diff(renamed, good) is not None
    # 2. a reordered pair
    reordered = list(good)
    reordered[10], reordered[11] = reordered[11], reordered[10]
    assert pc._diff(reordered, good) is not None
    # 3. a reserved->real field (extra column, same neighbours)
    longer = list(good)
    longer.insert(5, ("S", "B", "was_reserved"))
    assert pc._diff(longer, good) is not None
    # 4. identical compares clean
    assert pc._diff(list(good), good) is None
    print("  guard catches rename / reorder / new-field; clean compares pass — OK")


def drift_guard_live():
    print("== drift guard (live headers) ==")
    if not os.path.isdir(pc.DEFAULT_PROTOCOL_DIR):
        print(f"  SKIP: headers not found at {pc.DEFAULT_PROTOCOL_DIR}")
        return
    assert pc.check(pc.DEFAULT_PROTOCOL_DIR), "schema drifted from headers"


if __name__ == "__main__":
    check_sizes()
    round_trip()
    drift_guard_negative()
    drift_guard_live()
    print("\nALL TESTS PASSED")
