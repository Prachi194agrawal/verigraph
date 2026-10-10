"""Minimal reader for SCIP indexes (https://github.com/sourcegraph/scip), as written by `scip-python`.

Only the fields needed to resolve call sites are decoded, straight from the protobuf wire format,
so no generated protobuf bindings are required:

    Index.documents (2) -> Document.relative_path (1), Document.occurrences (2)
    Occurrence.range (1, packed int32), Occurrence.symbol (2), Occurrence.symbol_roles (3)
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROLE_DEFINITION = 0x1


@dataclass
class Occurrence:
    line: int  # 0-based
    start: int  # 0-based character
    end: int
    symbol: str
    roles: int

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)


@dataclass
class ScipDocument:
    path: str
    occurrences: list[Occurrence] = field(default_factory=list)


def _varint(buf: memoryview, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf: memoryview):
    """Yield (field_number, wire_type, value) for one message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value = buf[i:i + length]
            i += length
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _occurrence(buf: memoryview) -> Occurrence | None:
    rng: list[int] = []
    symbol, roles = "", 0
    for number, wire, value in _fields(buf):
        if number == 1:
            if wire == 2:  # packed
                j = 0
                while j < len(value):
                    v, j = _varint(value, j)
                    rng.append(v)
            else:
                rng.append(value)
        elif number == 2 and wire == 2:
            symbol = bytes(value).decode("utf-8", "replace")
        elif number == 3 and wire == 0:
            roles = value
    if len(rng) == 3:  # [line, start, end] on one line
        return Occurrence(rng[0], rng[1], rng[2], symbol, roles)
    if len(rng) == 4:  # [start_line, start, end_line, end]
        return Occurrence(rng[0], rng[1], rng[3] if rng[2] == rng[0] else rng[1] + 1, symbol, roles)
    return None


def decode_index(data: bytes) -> list[ScipDocument]:
    docs: list[ScipDocument] = []
    for number, wire, value in _fields(memoryview(data)):
        if number != 2 or wire != 2:
            continue
        doc = ScipDocument(path="")
        for dn, dw, dv in _fields(value):
            if dn == 1 and dw == 2:
                doc.path = bytes(dv).decode("utf-8", "replace")
            elif dn == 2 and dw == 2:
                occ = _occurrence(dv)
                if occ is not None and occ.symbol:
                    doc.occurrences.append(occ)
        docs.append(doc)
    return docs


# -- encoder, used by tests to build tiny indexes --------------------------------------------------------

def _enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_field(number: int, payload: bytes) -> bytes:
    return _enc_varint((number << 3) | 2) + _enc_varint(len(payload)) + payload


def encode_index(docs: list[ScipDocument]) -> bytes:
    out = bytearray()
    for doc in docs:
        body = bytearray(_enc_field(1, doc.path.encode()))
        for occ in doc.occurrences:
            packed = b"".join(_enc_varint(v) for v in (occ.line, occ.start, occ.end))
            msg = _enc_field(1, packed) + _enc_field(2, occ.symbol.encode()) + _enc_varint(3 << 3) + _enc_varint(occ.roles)
            body += _enc_field(2, msg)
        out += _enc_field(2, bytes(body))
    return bytes(out)
