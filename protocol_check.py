"""
protocol_check.py — guard against schema drift in a LIVING protocol.

The decode schema in bin_to_csv.py is a hand-mirror of the common-protocol C++
headers (the source of truth). This module re-derives each record's decode
contract DIRECTLY from those headers and compares it to bin_to_csv's schema, so
any drift fails loudly — not only size changes (which selftest's static_assert
mirror already catches) but also same-size changes: field reorders, renames, type
changes, and a `reserved` byte becoming a real field.

It is a deliberately small parser for the clean POD headers this repo uses
(single-identifier types, enums with explicit underlying type, 1-byte bitfield
structs, and arrays sized by literals or `inline constexpr` counts). It is NOT a
general C++ parser; if the headers adopt a construct it doesn't understand it
raises a clear error rather than silently passing.

Comparison contract — each record flattens to an ordered list of tokens:
    ('S', fmt_char, leaf_name)   a scalar / enum column
    ('b', leaf_name, width)      one bitfield bit (named bits only)
Padding / `reserved*` fields and reserved bits are dropped on BOTH sides. Only the
leaf name (final identifier, with array index for primitive arrays) is compared,
so bin_to_csv's cosmetic prefixes (valve0., tc0., …) don't cause false mismatches.

Usage:
    py -3.12 protocol_check.py [--protocol DIR]
Default protocol dir: $COMMON_PROTOCOL_DIR or the known checkout path.
"""

import argparse
import glob
import os
import re
import sys

import bin_to_csv as b

DEFAULT_PROTOCOL_DIR = os.environ.get(
    "COMMON_PROTOCOL_DIR",
    r"C:\Users\pitch\Documents\GitHub\common-protocol",
)

PRIM = {
    "uint8_t": ("B", 1), "int8_t": ("b", 1),
    "uint16_t": ("H", 2), "int16_t": ("h", 2),
    "uint32_t": ("I", 4), "int32_t": ("i", 4),
}

# Top C++ record struct for each (board, kind) bin_to_csv knows about.
RECORD_STRUCTS = {
    ("fcu", "sys"): "FcuSystemState",
    ("ecu", "sys"): "EcuSystemState",
    ("fcu", "ext"): "FcuExtendedSystemState",
    ("ecu", "ext"): "EcuExtendedSystemState",
}

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_STRUCT = re.compile(r"\bstruct\s+(\w+)\s*\{([^{}]*)\}\s*;", re.DOTALL)
_ENUM = re.compile(r"\benum\s+class\s+(\w+)\s*:\s*(\w+)\s*\{([^{}]*)\}\s*;", re.DOTALL)
_CONST = re.compile(r"\binline\s+constexpr\s+\w+\s+(\w+)\s*=\s*([0-9][0-9a-fA-FxX]*)\s*[uU]?\s*;")

_BITFIELD = re.compile(r"^([\w:]+)\s+(\w+)\s*:\s*(\d+)$")
_ARRAY = re.compile(r"^([\w:]+)\s+(\w+)\s*\[\s*(\w+)\s*\]$")
_SCALAR = re.compile(r"^([\w:]+)\s+(\w+)$")


class ProtocolModel:
    """Parsed structs / enums / constants from a header tree."""

    def __init__(self):
        self.structs = {}   # name -> {'kind': 'bitfield'|'composite', 'members': [...]}
        self.enums = {}     # name -> fmt char
        self.consts = {}    # name -> int

    def load_dir(self, root):
        files = glob.glob(os.path.join(root, "**", "*.hpp"), recursive=True)
        if not files:
            raise FileNotFoundError(f"no .hpp files under {root!r}")
        for path in files:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
            src = _COMMENT_BLOCK.sub("", src)
            src = _COMMENT_LINE.sub("", src)
            self._parse(src)
        return self

    def _parse(self, src):
        for name, value in _CONST.findall(src):
            try:
                self.consts[name] = int(value, 0)
            except ValueError:
                pass
        for name, underlying, _body in _ENUM.findall(src):
            if underlying not in PRIM:
                raise ValueError(f"enum {name}: unsupported underlying type {underlying!r}")
            self.enums[name] = PRIM[underlying][0]
        for name, body in _STRUCT.findall(src):
            # Defer member-parse errors: structs we never flatten (e.g.
            # SdBlockView, which uses std::span) must not break the check.
            try:
                self.structs[name] = self._parse_struct_body(body)
            except ValueError as e:
                self.structs[name] = {"kind": "unparsable", "error": str(e)}

    def _parse_struct_body(self, body):
        members = []
        is_bitfield = False
        for raw in body.split(";"):
            decl = " ".join(raw.split()).strip()
            if not decl:
                continue
            m = _BITFIELD.match(decl)
            if m:
                is_bitfield = True
                members.append({"kind": "bit", "name": m.group(2), "width": int(m.group(3))})
                continue
            m = _ARRAY.match(decl)
            if m:
                members.append({"kind": "field", "type": m.group(1),
                                "name": m.group(2), "count": m.group(3)})
                continue
            m = _SCALAR.match(decl)
            if m:
                members.append({"kind": "field", "type": m.group(1),
                                "name": m.group(2), "count": None})
                continue
            raise ValueError(f"unparsable struct member: {decl!r}")
        return {"kind": "bitfield" if is_bitfield else "composite", "members": members}

    def _count(self, expr):
        if expr is None:
            return 1
        if expr.isdigit():
            return int(expr)
        if expr in self.consts:
            return self.consts[expr]
        raise ValueError(f"unknown array count {expr!r} (not a literal or known constexpr)")

    @staticmethod
    def _is_reserved(name):
        return name == "reserved" or name.startswith("reserved")

    def flatten(self, type_name):
        """Flatten a struct type into the ordered token contract."""
        if type_name not in self.structs:
            raise ValueError(f"type {type_name!r} not found in headers")
        s = self.structs[type_name]
        if s["kind"] == "unparsable":
            raise ValueError(f"type {type_name!r} failed to parse: {s['error']}")
        tokens = []
        if s["kind"] == "bitfield":
            for m in s["members"]:
                if not self._is_reserved(m["name"]):
                    tokens.append(("b", m["name"], m["width"]))
            return tokens
        for m in s["members"]:
            if self._is_reserved(m["name"]):
                continue
            count = self._count(m["count"])
            t = m["type"]
            for k in range(count):
                if t in PRIM:
                    name = m["name"] if m["count"] is None else f"{m['name']}[{k}]"
                    tokens.append(("S", PRIM[t][0], name))
                elif t in self.enums:
                    name = m["name"] if m["count"] is None else f"{m['name']}[{k}]"
                    tokens.append(("S", self.enums[t], name))
                elif t in self.structs:
                    tokens.extend(self.flatten(t))
                else:
                    raise ValueError(f"field {m['name']}: unknown type {t!r}")
        return tokens


def _leaf(name):
    return name.split(".")[-1]


def python_tokens(board, kind):
    """The decode contract bin_to_csv.py actually uses, in token form."""
    builder, _exp = b.RECORDS[(board, kind)]
    tokens = []
    for el in builder():
        if el[0] == "pad":
            continue
        if el[0] == "prim":
            _, name, fmt = el
            tokens.append(("S", fmt, _leaf(name)))
        elif el[0] == "bits":
            for (name, width) in el[1]:
                if name is not None:
                    tokens.append(("b", _leaf(name), width))
    return tokens


def _diff(a, b_):
    for i, (x, y) in enumerate(zip(a, b_)):
        if x != y:
            return i, x, y
    if len(a) != len(b_):
        i = min(len(a), len(b_))
        return i, (a[i] if i < len(a) else None), (b_[i] if i < len(b_) else None)
    return None


def check(protocol_dir):
    model = ProtocolModel().load_dir(protocol_dir)
    all_ok = True
    print(f"== protocol drift check ==\n  headers: {protocol_dir}")
    for (board, kind), struct_name in RECORD_STRUCTS.items():
        hdr = model.flatten(struct_name)
        py = python_tokens(board, kind)
        d = _diff(py, hdr)
        if d is None:
            print(f"  {board}/{kind} ({struct_name}): OK  ({len(hdr)} columns)")
        else:
            all_ok = False
            i, ptok, htok = d
            print(f"  {board}/{kind} ({struct_name}): DRIFT at column {i}")
            print(f"      bin_to_csv: {ptok}")
            print(f"      header    : {htok}")
            print(f"      (python {len(py)} cols vs header {len(hdr)} cols)")
    return all_ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--protocol", default=DEFAULT_PROTOCOL_DIR,
                    help="path to the common-protocol header checkout")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.protocol):
        print(f"protocol dir not found: {args.protocol}\n"
              f"pass --protocol DIR or set COMMON_PROTOCOL_DIR.", file=sys.stderr)
        return 2
    return 0 if check(args.protocol) else 1


if __name__ == "__main__":
    sys.exit(main())
