#!/usr/bin/env python3
"""
bin_to_csv.py — convert common-protocol SD telemetry logs (.bin) to CSV.

The firmware (repo: common-protocol) writes three logs per board:

    data_fast.bin  -> SystemState          (high rate)
    data_slow.bin  -> SystemState          (downsampled / averaged)
    data_ext.bin   -> ExtendedSystemState  (~10 Hz, bulky/slow state)

and there is one set per board (FCU / ECU), so up to six files. The record
layout differs by (board, kind):

    SystemState           FCU = FcuSystemState           (60 bytes)
                          ECU = EcuSystemState           (56 bytes)
    ExtendedSystemState   FCU = FcuExtendedSystemState  (132 bytes)
                          ECU = EcuExtendedSystemState   (48 bytes)

On-disk format (see telemetry/sd_block_footer.hpp). Each file is a stream of
fixed SD_LOG_BLOCK_BYTES (16384) blocks:

    [ records... ][ MAGIC fill ][ trailer: saved_ms u32, payload_bytes u32, crc32 u32 ]

`payload_bytes` is a whole number of records; `crc32` is the zlib/reflected
CRC-32 over [0, total-4). We verify it (decode anyway on mismatch, flagging
crc_ok=0) and emit one CSV row per record.

Output is RAW: enums and states are written as their raw on-wire integers,
bitfield status bytes are expanded into one 0/1/N integer column per named bit,
and pure padding/reserved bytes are dropped. Two context columns are prepended:
`crc_ok` (block CRC passed) and `block_saved_ms` (the block trailer timestamp).

Usage:
    python bin_to_csv.py FILE_OR_DIR [FILE_OR_DIR ...] [-o OUTDIR]
                         [--board fcu|ecu] [--kind sys|ext]

Board/kind are inferred from each filename (substrings fcu/ecu and
fast/slow/ext); pass --board / --kind to override when the name is ambiguous.
"""

import argparse
import os
import struct
import sys
import zlib
import csv

# --- on-disk block constants (telemetry/sd_block_footer.hpp) ---------------
SD_LOG_BLOCK_BYTES = 16384
SD_BLOCK_TRAILER_BYTES = 12
SD_BLOCK_MIN_FOOTER_BYTES = 4 + SD_BLOCK_TRAILER_BYTES  # one MAGIC word + trailer

# ---------------------------------------------------------------------------
# Record schema. Each record is an ordered list of "elements":
#   ('prim', name, fmt)  -> one column; fmt is a struct char (little-endian,
#                           standard sizes): B/H/I unsigned 1/2/4, b/h/i signed.
#   ('pad',  n)          -> n bytes of padding, no column.
#   ('bits', [(name, width), ...]) -> exactly one byte; emits one integer column
#                           per named field (name=None => reserved, consumed but
#                           not emitted). widths must sum to 8.
# ---------------------------------------------------------------------------

def adc_info(p):
    e = [('prim', p + 'state', 'B'),
         ('bits', [(p + 'status.initialized', 1), (p + 'status.data_valid', 1), (None, 6)]),
         ('pad', 2)]
    for i in range(8):  # ADC_CHANNEL_COUNT
        e.append(('prim', f'{p}channels[{i}]', 'i'))
    return e


def valve_info(p):
    return [('prim', p + 'state', 'B'),
            ('bits', [(p + 'status.initialized', 1),
                      (p + 'status.open_limit_high', 1),
                      (p + 'status.closed_limit_high', 1),
                      (p + 'status.in_transition', 1),
                      (p + 'status.fault_both_switches', 1),
                      (None, 3)]),
            ('prim', p + 'current_set_value', 'B')]


def storage_info(p):
    return [('prim', p + 'state', 'B'),
            ('bits', [(p + 'status.initialized', 1),
                      (p + 'status.plugged_in', 1),
                      (p + 'status.error', 2),
                      (None, 4)]),
            ('prim', p + 'overrun_count', 'H')]


def can_info(p):
    return [('prim', p + 'state', 'B'),
            ('bits', [(p + 'status.initialized', 1), (p + 'status.tx_error', 1), (None, 6)]),
            ('prim', p + 'rx_dropped', 'H')]


def ethernet_info(p):
    return [('prim', p + 'state', 'B'),
            ('bits', [(p + 'status.initialized', 1),
                      (p + 'status.tx_busy', 1),
                      (p + 'status.tx_error', 1),
                      (None, 5)]),
            ('prim', p + 'rx_dropped', 'H')]


def system_state_base(p=''):
    e = [('prim', p + 'creation_timestamp_ms', 'I')]
    e += adc_info(p + 'adc.')
    e += valve_info(p + 'valve0.')
    e += valve_info(p + 'valve1.')
    e += storage_info(p + 'storage.')
    e += can_info(p + 'can.')
    e += [('pad', 2)]
    return e


def refused_command_info(p):
    return [('prim', p + 'set_flag_id', 'H'),
            ('prim', p + 'set_flag_value', 'B'),
            ('prim', p + 'set_flag_state', 'B'),
            ('prim', p + 'set_state_from', 'B'),
            ('prim', p + 'set_state_to', 'B'),
            ('pad', 2),
            ('prim', p + 'set_state_refused_count', 'H'),
            ('prim', p + 'set_flag_refused_count', 'H'),
            ('prim', p + 'set_valve_id', 'B'),
            ('prim', p + 'set_valve_action', 'B'),
            ('prim', p + 'set_valve_value', 'B'),
            ('prim', p + 'set_valve_state', 'B'),
            ('prim', p + 'set_valve_refused_count', 'H'),
            ('pad', 2)]


def sd_write_engine_info(p):
    return [('prim', p + 'overrun_count', 'H'),
            ('prim', p + 'errored', 'B'),
            ('prim', p + 'card_detected', 'B')]


def extended_base(p=''):
    e = [('prim', p + 'creation_timestamp_ms', 'I'),
         ('prim', p + 'control_flags_base', 'B'),
         ('prim', p + 'control_flags_board', 'B'),
         ('prim', p + 'backup_status', 'B'),
         ('prim', p + 'seconds_since_last_ping', 'B')]
    e += refused_command_info(p + 'refused.')
    e += sd_write_engine_info(p + 'sd_write.')
    return e


def ematch_info(p):
    return [('bits', [(p + 'status.detected', 1), (p + 'status.energised', 1), (None, 6)]),
            ('pad', 3),
            ('prim', p + 'last_energised_ms', 'I'),
            ('prim', p + 'last_deenergised_ms', 'I')]


def solenoid_info(p):
    return [('bits', [(p + 'status.detected', 1), (p + 'status.open', 1), (None, 6)]),
            ('pad', 3),
            ('prim', p + 'last_opened_ms', 'I'),
            ('prim', p + 'last_closed_ms', 'I')]


def heater_info(p):
    return [('bits', [(p + 'status.on', 1), (None, 7)]),
            ('pad', 3),
            ('prim', p + 'last_on_ms', 'I'),
            ('prim', p + 'last_off_ms', 'I')]


def thermocouple_info(p):
    return [('prim', p + 'state', 'B'),
            ('bits', [(p + 'status.open_circuit', 1),
                      (p + 'status.over_under_v', 1),
                      (p + 'status.tc_out_range', 1),
                      (p + 'status.cj_out_range', 1),
                      (p + 'status.data_valid', 1),
                      (p + 'status.comms_ok', 1),
                      (None, 2)]),
            ('pad', 2),
            ('prim', p + 'thermocouple_code', 'i'),
            ('prim', p + 'cold_junction_code', 'i')]


def power_monitor_info(p):
    e = [('prim', p + 'state', 'B'),
         ('bits', [(p + 'status.data_valid', 1), (p + 'status.read_error', 1), (None, 6)]),
         ('pad', 2)]
    for i in range(3):  # POWER_MONITOR_CHANNEL_COUNT
        e.append(('prim', f'{p}channels[{i}].shunt_code', 'h'))
        e.append(('prim', f'{p}channels[{i}].bus_code', 'h'))
    return e


def fcu_system_state():
    return system_state_base('') + ethernet_info('eth.')


def ecu_system_state():
    return system_state_base('')


def fcu_extended_system_state():
    e = extended_base('')
    e += ematch_info('ematch.')
    e += solenoid_info('solenoid.')
    e += heater_info('heater.')
    for i in range(4):  # THERMOCOUPLE_COUNT
        e += thermocouple_info(f'tc{i}.')
    e += power_monitor_info('power.')
    return e


def ecu_extended_system_state():
    return extended_base('') + power_monitor_info('power.')


# (board, kind) -> (elements builder, expected record size in bytes)
RECORDS = {
    ('fcu', 'sys'): (fcu_system_state, 60),
    ('ecu', 'sys'): (ecu_system_state, 56),
    ('fcu', 'ext'): (fcu_extended_system_state, 132),
    ('ecu', 'ext'): (ecu_extended_system_state, 48),
}


def compile_record(elements):
    """Turn an element list into (struct fmt, header, instructions, size).

    instructions: list of (op, value_index, shift, mask) — one per output
    column. op 0 = take unpacked value as-is; op 1 = (value >> shift) & mask.
    """
    fmt = '<'
    header = []
    instructions = []
    vidx = 0
    for el in elements:
        if el[0] == 'pad':
            fmt += 'x' * el[1]
        elif el[0] == 'prim':
            _, name, f = el
            fmt += f
            header.append(name)
            instructions.append((0, vidx, 0, 0))
            vidx += 1
        elif el[0] == 'bits':
            fmt += 'B'
            bitpos = 0
            total = 0
            for (name, width) in el[1]:
                if name is not None:
                    header.append(name)
                    instructions.append((1, vidx, bitpos, (1 << width) - 1))
                bitpos += width
                total += width
            if total != 8:
                raise ValueError(f'bitfield widths sum to {total}, not 8: {el[1]}')
            vidx += 1
        else:
            raise ValueError(f'unknown element {el!r}')
    return fmt, header, instructions, struct.calcsize(fmt)


def detect(path, board_override, kind_override):
    name = os.path.basename(path).lower()
    board = board_override
    kind = kind_override
    if board is None:
        if 'ecu' in name:
            board = 'ecu'
        elif 'fcu' in name:
            board = 'fcu'
    if kind is None:
        if 'ext' in name:
            kind = 'ext'
        elif 'fast' in name or 'slow' in name or 'sys' in name:
            kind = 'sys'
    return board, kind


def convert_file(path, outpath, board, kind, recover=False):
    builder, expected = RECORDS[(board, kind)]
    fmt, fields, instructions, size = compile_record(builder())
    if size != expected:
        raise RuntimeError(
            f'{path}: compiled record size {size} != expected {expected} '
            f'for {board}/{kind} — schema mismatch')

    rec = struct.Struct(fmt)
    header = ['crc_ok', 'block_saved_ms'] + fields

    total_bytes = os.path.getsize(path)
    n_blocks = 0
    n_records = 0
    n_skipped = 0       # blocks skipped in recover mode (invalid / foreign)
    n_partial = 0
    stopped_at = None   # block index where decoding stopped
    stop_reason = None  # why it stopped (crc / foreign-format / older-run / eof)

    with open(path, 'rb') as fin, open(outpath, 'w', newline='') as fout:
        writer = csv.writer(fout)
        writer.writerow(header)
        last_pct = -1
        read = 0
        file_block_index = 0  # 0-based index over every block read from the file
        prev_saved = None     # last accepted block's saved_ms (run monotonicity)
        while True:
            block = fin.read(SD_LOG_BLOCK_BYTES)
            if not block:
                break
            read += len(block)
            if len(block) < SD_BLOCK_MIN_FOOTER_BYTES:
                print(f'  warning: trailing {len(block)} bytes too small for a '
                      f'block footer — skipped', file=sys.stderr)
                break
            if len(block) < SD_LOG_BLOCK_BYTES:
                n_partial += 1  # short final chunk; decode best-effort if valid

            total = len(block)
            trailer_off = total - SD_BLOCK_TRAILER_BYTES
            saved_ms, payload_bytes, crc32 = struct.unpack_from(
                '<III', block, trailer_off)
            crc_ok = (zlib.crc32(block[:total - 4]) & 0xFFFFFFFF) == crc32

            # Decide whether this block belongs to THIS run. The file is pre-allocated
            # and may hold leftovers from earlier/other runs (incl. the OTHER board's
            # records, which still pass CRC) past the last written block. A current-run
            # block must: pass CRC, carry a whole number of THIS record's bytes, and not
            # jump backwards in time (a backward jump = an older run's blocks begin).
            reason = None
            if not crc_ok:
                reason = 'crc'                       # corruption / zero-fill tail
            elif payload_bytes > trailer_off or payload_bytes % size != 0:
                reason = 'foreign-format'            # e.g. stale ECU data in an FCU file
            elif prev_saved is not None and saved_ms < prev_saved:
                reason = 'older-run'                 # backward timestamp jump

            if reason is not None:
                if not recover:
                    stopped_at = file_block_index
                    stop_reason = reason
                    break
                # recover: skip this block, keep scanning for more current-format
                # blocks. Foreign (other-board) and invalid blocks are NEVER decoded.
                n_skipped += 1
                file_block_index += 1
                continue

            prev_saved = saved_ms
            n_blocks += 1
            n_full = payload_bytes // size
            if n_full:
                mv = memoryview(block)[:n_full * size]
                rows = []
                append = rows.append
                for vals in rec.iter_unpack(mv):
                    append([1, saved_ms] +
                           [vals[i] if op == 0 else (vals[i] >> sh) & mask
                            for (op, i, sh, mask) in instructions])
                writer.writerows(rows)
                n_records += n_full

            file_block_index += 1
            if total_bytes:
                pct = int(read * 100 / total_bytes)
                if pct != last_pct and pct % 10 == 0:
                    print(f'\r  {os.path.basename(path)}: {pct}%', end='',
                          file=sys.stderr)
                    last_pct = pct

    print(f'\r  {os.path.basename(path)}: done', file=sys.stderr)
    if n_partial:
        print(f'  warning: {n_partial} partial/truncated block(s) decoded '
              f'best-effort', file=sys.stderr)
    return n_blocks, n_records, n_skipped, stopped_at, stop_reason


def gather_inputs(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith('.bin'):
                    files.append(os.path.join(p, name))
        else:
            files.append(p)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description='Convert common-protocol .bin telemetry logs to CSV.')
    ap.add_argument('inputs', nargs='+', help='.bin files or directories containing them')
    ap.add_argument('-o', '--outdir', help='output directory (default: next to each input)')
    ap.add_argument('--board', choices=['fcu', 'ecu'], help='override board detection')
    ap.add_argument('--kind', choices=['sys', 'ext'], help='override record-kind detection')
    ap.add_argument('--recover', action='store_true',
                    help='scan the entire file instead of stopping at the first CRC '
                         'failure; emit every CRC-valid block and skip invalid ones '
                         '(use to recover valid data written after a corrupt block/gap)')
    args = ap.parse_args(argv)

    files = gather_inputs(args.inputs)
    if not files:
        print('No .bin files found.', file=sys.stderr)
        return 1
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    rc = 0
    for path in files:
        if not os.path.isfile(path):
            print(f'skip (not a file): {path}', file=sys.stderr)
            rc = 1
            continue
        board, kind = detect(path, args.board, args.kind)
        if board is None or kind is None:
            print(f'skip {path}: could not detect '
                  f'board={board} kind={kind} from filename — '
                  f'pass --board and/or --kind', file=sys.stderr)
            rc = 1
            continue

        base = os.path.splitext(os.path.basename(path))[0]
        outdir = args.outdir or os.path.dirname(os.path.abspath(path))
        outpath = os.path.join(outdir, base + '.csv')

        print(f'{path}  [{board}/{kind}] -> {outpath}', file=sys.stderr)
        try:
            blocks, records, skipped, stopped_at, reason = convert_file(
                path, outpath, board, kind, recover=args.recover)
        except Exception as e:
            print(f'  error: {e}', file=sys.stderr)
            rc = 1
            continue
        reason_text = {
            'crc': 'CRC failure / zero-fill tail',
            'foreign-format': "other board's / older record format",
            'older-run': 'older run (timestamp jumped back)',
        }.get(reason, reason)
        msg = f'  {records} records, {blocks} block(s) (this run)'
        if stopped_at is not None:
            msg += f'; stopped at block {stopped_at} ({reason_text})'
        if skipped:
            msg += f'; {skipped} non-current block(s) skipped'
        print(msg, file=sys.stderr)

    return rc


if __name__ == '__main__':
    sys.exit(main())
