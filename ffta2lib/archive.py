"""
FFTA2 (DS) "pc.bin"/"pc.idx" master archive.

The ROM stores nearly all of its assets inside `master/pc.bin` (a flat blob)
plus `master/pc.idx` (the index).  Files are NOT stored under their real
names -- the game looks them up by hashing the (virtual) path with a 10 bit
CRC and indexing a 1024 slot table.

Layout of pc.idx:
    u32 num_slots
    u32 (unused)
    num_slots * { u32 a, u32 b, u8 c }        # 9 bytes each
    ... "bottom table" ...

Slot encoding (from a/b):
    offset = (a & 0x3FFFFF) * 2
    size   = ((a >> 26) + (b & 0xFFFF) * 64) * 4

If (a & 1) the slot is a *list* of files: the decoded offset points into the
bottom table of pc.idx (absolute offset / 4), and the bottom table entry
describes the individual members.

Bottom table:
    u8 num_entries            (0 terminates the table)
    per entry:
        u32 offset_in_4byte_units
        u32 size_in_4byte_units   (only for entries after the first)
        u32 (skipped)
        u8  chr                    (expected character of the file name)
        u8  chr_position           (index into the file name)
        u8  (skipped)
"""

import struct
from typing import List, NamedTuple, Optional


def filename_crc(name: str) -> int:
    """10 bit hash of a virtual path, as used by the game."""
    b = name.encode("utf-8")
    if not b:
        return 0
    crc = b[0]
    for i in range(1, len(b)):
        crc = (crc * 0x25 + b[i]) & 0x3FF
    return crc & 0x3FF


class Slot(NamedTuple):
    index: int
    offset: int          # offset into pc.bin
    size: int            # size in bytes
    is_list: bool
    members: Optional[List]  # list of Member for list slots


class Member(NamedTuple):
    offset: int
    size: int
    chr: int
    chr_position: int


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


class Archive:
    def __init__(self, idx_path: str, bin_path: str):
        with open(idx_path, "rb") as f:
            self.idx = f.read()
        with open(bin_path, "rb") as f:
            self.bin = f.read()

        self.num_slots = _u32(self.idx, 0)
        self.extra_start = 8 + self.num_slots * 9
        self.slots: List[Optional[Slot]] = [None] * self.num_slots
        self._bottom = self._parse_bottom_table()
        self._build_slots()

    # ------------------------------------------------------------------
    def _parse_bottom_table(self) -> dict:
        """offset relative to extra_start -> list[Member]"""
        out = {}
        pos = self.extra_start
        end = len(self.idx)
        while pos < end:
            start = pos - self.extra_start
            num = self.idx[pos]
            pos += 1
            if num == 0:
                break
            members = []
            for i in range(num):
                offset = _u32(self.idx, pos)
                pos += 4
                size = 0
                if i != 0:
                    size = _u32(self.idx, pos)
                    pos += 4
                    pos += 4  # skipped
                chr_ = self.idx[pos]
                chr_pos = self.idx[pos + 1]
                pos += 3
                members.append(Member(offset * 4, size * 4, chr_, chr_pos))
            out[start] = members
        return out

    def _build_slots(self):
        off = 8
        for i in range(self.num_slots):
            a = _u32(self.idx, off)
            b = _u32(self.idx, off + 4)
            off += 9
            if a == 0 and b == 0:
                self.slots[i] = None
                continue
            offset = (a & 0x3FFFFFF) * 2
            size = ((a >> 26) + (b & 0xFFFF) * 64) * 4
            if a & 1:
                index = offset // 4 - self.extra_start
                members = self._bottom.get(index)
                if members is None:
                    members = []
                self.slots[i] = Slot(i, offset, size, True, members)
            else:
                self.slots[i] = Slot(i, offset, size, False, None)

    # ------------------------------------------------------------------
    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset > len(self.bin):
            return b""
        return self.bin[offset:offset + size]

    def get(self, slot_index: int):
        """Return list of (bytes, chr, chr_position) for a slot."""
        slot = self.slots[slot_index]
        if slot is None:
            return []
        if not slot.is_list:
            return [(self.read(slot.offset, slot.size), -1, -1)]
        out = []
        for m in slot.members:
            size = m.size if m.size != 0 else slot.size
            out.append((self.read(m.offset, size), m.chr, m.chr_position))
        return out

    def get_named(self, name: str):
        """Lookup by virtual path; returns list of candidate byte blobs."""
        return self.get(filename_crc(name))

    def get_one(self, name: str) -> Optional[bytes]:
        """Lookup by virtual path honouring the chr/chr_position disambiguation."""
        want = name.encode("utf-8")
        for data, chr_, chr_pos in self.get_named(name):
            if chr_pos < 0:
                return data
            if chr_pos < len(want) and want[chr_pos] == chr_:
                return data
        return None

    def member_bytes(self, key):
        """The content of the member "slot:member"."""
        slot, member = (int(x) for x in key.split(":"))
        members = self.get(slot)
        if not members or member >= len(members):
            raise KeyError("no such member: %s" % key)
        return members[member][0]

    def all_entries(self):
        """Yield (slot_index, member_index, data, chr, chr_position)."""
        for i in range(self.num_slots):
            slot = self.slots[i]
            if slot is None:
                continue
            for j, (data, chr_, chr_pos) in enumerate(self.get(i)):
                yield i, j, data, chr_, chr_pos


# ------------------------------------------------------------------ writer ---
class ArchiveWriter:
    """Rebuilds the "pc.bin"/"pc.idx" master archive.

    The index is patched rather than regenerated: the slot table, the order
    of the bottom table and every field the game does not use are kept
    exactly as they were.  Only the offsets and sizes of the members that
    actually changed are updated, so a rebuild without changes is byte
    identical to the original.

    ``members`` maps (slot, member) to the new content of that member.
    """

    def __init__(self, idx_bytes: bytes, bin_bytes: bytes):
        self.idx = idx_bytes
        self.bin = bin_bytes
        self.num_slots = _u32(idx_bytes, 0)
        self.extra_start = 8 + self.num_slots * 9
        self.headers = [struct.unpack_from("<IIB", idx_bytes, 8 + i * 9)
                        for i in range(self.num_slots)]
        # ---- bottom table ------------------------------------------------
        self.entries = []          # list of (pos, [member dicts])
        pos = self.extra_start
        while pos < len(idx_bytes):
            start = pos
            num = idx_bytes[pos]
            if num == 0:
                self.bottom_end = pos      # the terminating zero byte
                break
            pos += 1
            members = []
            for i in range(num):
                off = _u32(idx_bytes, pos)
                pos += 4
                size = 0
                skip = 0
                if i != 0:
                    size = _u32(idx_bytes, pos)
                    pos += 4
                    skip = _u32(idx_bytes, pos)
                    pos += 4
                chr_ = idx_bytes[pos]
                chr_pos = idx_bytes[pos + 1]
                tail = idx_bytes[pos + 2]
                pos += 3
                members.append(dict(off=off, size=size, skip=skip,
                                    chr=chr_, chr_pos=chr_pos, tail=tail))
            self.entries.append((start, members))
        self.entries_by_pos = dict(self.entries)
        self.bottom_end = pos
        # ---- slots -------------------------------------------------------
        self.entry_of_slot = {}
        self.slot_of_entry = {}
        for i, (a, b, c) in enumerate(self.headers):
            if a == 0 and b == 0:
                continue
            if a & 1:
                entry_pos = (a & 0x3FFFFFF) // 2
                self.entry_of_slot[i] = entry_pos
                self.slot_of_entry[entry_pos] = i
        # ---- layout order ------------------------------------------------
        groups = []
        for i, (a, b, c) in enumerate(self.headers):
            if a == 0 and b == 0:
                continue
            if a & 1:
                members = self.entries_by_pos.get(self.entry_of_slot.get(i))
                if members is None:
                    continue
                groups.append((members[0]["off"] * 4, i, "list", members))
            else:
                groups.append(((a & 0x3FFFFFF) * 2, i, "single", None))
        groups.sort(key=lambda g: g[0])
        self.groups = groups

    # ------------------------------------------------------------------
    def member_data(self, slot, member, overrides=None):
        """The (possibly overridden) content of one member."""
        if overrides and (slot, member) in overrides:
            return overrides[(slot, member)]
        a, b, c = self.headers[slot]
        if a & 1:
            members = self.entries_by_pos.get(self.entry_of_slot.get(slot), [])
            m = members[member]
            size = m["size"] * 4
            if member == 0:
                size = ((a >> 26) + (b & 0xFFFF) * 64) * 4
            return self.bin[m["off"] * 4:m["off"] * 4 + size]
        off = (a & 0x3FFFFFF) * 2
        size = ((a >> 26) + (b & 0xFFFF) * 64) * 4
        return self.bin[off:off + size]

    def build(self, overrides=None):
        """Return (idx_bytes, bin_bytes) with the overrides applied."""
        overrides = overrides or {}
        out = bytearray()
        new_offsets = {}      # slot -> new offset of the first member
        new_sizes = {}        # slot -> new size of the first member
        member_offsets = {}   # (slot, member) -> new offset
        member_sizes = {}     # (slot, member) -> new size
        for (start, slot, kind, members) in self.groups:
            if kind == "single":
                data = self.member_data(slot, 0, overrides)
                data = data + b"\x00" * (-len(data) % 4)
                member_offsets[(slot, 0)] = len(out)
                member_sizes[(slot, 0)] = len(data)
                out += data
                new_offsets[slot] = len(out) - len(data)
                new_sizes[slot] = len(data)
                if not data:
                    # an empty file keeps the offset it had in the original
                    new_offsets[slot] = start
            else:
                base = len(out)
                off = base
                for k, m in enumerate(members):
                    data = self.member_data(slot, k, overrides)
                    data = data + b"\x00" * (-len(data) % 4)
                    member_offsets[(slot, k)] = off
                    member_sizes[(slot, k)] = len(data)
                    off += len(data)
                    out += data
                new_offsets[slot] = base
                new_sizes[slot] = member_sizes[(slot, 0)]
                if not out[base:]:
                    # an empty group keeps the offset it had in the original
                    new_offsets[slot] = start

        # ---- index -------------------------------------------------------
        idx = bytearray()
        idx += struct.pack("<II", self.num_slots, 0)
        for i, (a, b, c) in enumerate(self.headers):
            if a == 0 and b == 0:
                idx += struct.pack("<IIB", 0, 0, c)
                continue
            size = new_sizes.get(i, ((a >> 26) + (b & 0xFFFF) * 64) * 4)
            units = (size + 3) // 4
            if a & 1:
                off_field = self.entry_of_slot[i] * 2 + 1
            else:
                off_field = new_offsets[i] // 2
            na = ((units % 64) << 26) | (off_field & 0xFFFFFFFF & 0x03FFFFFF)
            nb = ((b & 0xFFFF0000) | (units // 64)) & 0xFFFFFFFF
            idx += struct.pack("<IIB", na & 0xFFFFFFFF, nb, c)
        # bottom table
        for (start, members) in self.entries:
            slot = self.slot_of_entry.get(start)
            idx += bytes([len(members)])
            for k, m in enumerate(members):
                idx += struct.pack("<I", member_offsets[(slot, k)] // 4)
                if k != 0:
                    idx += struct.pack("<I", (member_sizes[(slot, k)] + 3) // 4)
                    idx += struct.pack("<I", m["skip"])
                idx += bytes((m["chr"], m["chr_pos"], m["tail"]))
        idx += self.idx[self.bottom_end:]
        return bytes(idx), bytes(out)
