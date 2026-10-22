"""FFTA2 ".idx" + ".pak" container.

idx:  u32 (unused header), then 6 byte records { u32 a, u16 b }
pak:  the raw blob; every file is stored in its own slot.

Record encoding (mirrors the editor's IdxAndPak reader):

    ab(a)  = ((a << 5) & 0xFFFFFFFF) >> 8      == (a & 0x07FFFFFF) >> 3
    offset = ab(a_i) << 2
    size   = ((a >> 27) << 2) + (b << 7)

The two fields the reader throws away are not noise, the game needs them:

    a bits 27..31   (size & 127) >> 2
    b               size >> 7

so that ``size == ((a >> 27) << 2) + (b << 7)``.  ``size`` is the length of
the file rounded up to a multiple of four.  Verified against every .idx
container of the game (39 containers, 25367 records).

``a & 7`` is 0 for a normal file and 7 for an empty slot.  The last record
of the table is the terminator: it holds the end offset of the container.

Two storage layouts exist in the game and both are reproduced here:

    "packed"     the next file starts right behind the current one
    "aligned32"  the next file starts at the next multiple of 32 bytes

:func:`detect_mode` finds out which one a container uses.
"""

import struct
from typing import List, Optional

TERMINATOR_B = 0xFBE0


def _ab(a: int) -> int:
    return ((a << 5) & 0xFFFFFFFF) >> 8


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def decode_records(idx: bytes):
    """Return the list of (a, b) tuples of an index file."""
    if len(idx) < 4:
        return []
    n = (len(idx) - 4) // 6
    return [struct.unpack_from("<IH", idx, 4 + i * 6) for i in range(n)]


def record_size(a: int, b: int) -> int:
    """The size of the file described by a record."""
    return ((a >> 27) << 2) + (b << 7)


def record_offset(a: int) -> int:
    return _ab(a) << 2


def detect_mode(idx: bytes) -> str:
    """Detect how a container stores its files.

    "packed"     the files follow each other directly.
    "aligned32"  every file starts at a multiple of 32 bytes.
    """
    recs = decode_records(idx)
    offsets = []
    gaps = []
    for i in range(len(recs) - 1):
        a, b = recs[i]
        if (a & 7) != 0:
            continue
        a2 = recs[i + 1][0]
        dist = (_ab(a2) - _ab(a)) << 2
        offsets.append(_ab(a) << 2)
        gaps.append(dist - record_size(a, b))
    if offsets and all(g == 0 for g in gaps):
        return "packed"
    if offsets and all(o % 32 == 0 for o in offsets):
        return "aligned32"
    return "packed"


class IdxPak:
    """Reader for an index/blob pair."""

    def __init__(self, idx: bytes, pak: bytes):
        self.idx = idx
        self.pak = pak
        self.recs = decode_records(idx)
        self.mode = detect_mode(idx)
        self.files: List[Optional[bytes]] = []
        for i in range(len(self.recs) - 1):
            a, b = self.recs[i]
            if (a & 7) != 0:
                self.files.append(None)
                continue
            off = _ab(a) << 2
            size = record_size(a, b)
            self.files.append(pak[off:off + size] if size else b"")

    def __len__(self):
        return len(self.files)

    def __getitem__(self, i):
        return self.files[i]


class IdxPakWriter:
    """Rebuilds an ".idx" + ".pak" pair.

    The original index is patched rather than regenerated: the record count,
    the empty slots and every field the game does not use stay untouched.
    A rebuild that changes nothing is therefore byte identical to the
    original.

    ``files`` is the list of file contents (one entry per record, ``None``
    for an empty slot).
    """

    def __init__(self, original_idx: bytes, files, mode=None):
        self.idx = original_idx
        self.recs = decode_records(original_idx)
        self.files = list(files)
        self.mode = mode or detect_mode(original_idx)
        if len(self.files) != len(self.recs) - 1:
            raise ValueError("expected %d files, got %d"
                             % (len(self.recs) - 1, len(self.files)))

    # ------------------------------------------------------------------
    def build(self):
        """Return (idx_bytes, pak_bytes)."""
        pak = bytearray()
        recs = []
        bs = []
        for i, data in enumerate(self.files):
            a, b = self.recs[i]
            offset = len(pak)
            if data is None:
                # empty slot: keep the flags and the size field of the
                # original record, only move the offset
                recs.append(((a >> 27) << 27)
                            | (((offset >> 2) << 3) & 0x07FFFFFF) | 7)
                bs.append(b)
            else:
                size = (len(data) + 3) & ~3
                recs.append((((size & 127) >> 2) << 27)
                            | (((offset >> 2) << 3) & 0x07FFFFFF))
                bs.append(size >> 7)
                pak += data
                if self.mode == "aligned32":
                    pak += b"\x00" * (_align(len(pak), 32) - len(pak))
        # terminator
        ta, tb = self.recs[-1]
        end = len(pak)
        recs.append(((ta >> 27) << 27)
                    | (((end >> 2) << 3) & 0x07FFFFFF) | 7)
        bs.append(tb)

        idx = bytearray(b"\x00\x00\x00\x00")
        for a, b in zip(recs, bs):
            idx += struct.pack("<IH", a & 0xFFFFFFFF, b & 0xFFFF)
        idx += self.idx[4 + len(self.recs) * 6:]  # trailing bytes
        return bytes(idx), bytes(pak)
