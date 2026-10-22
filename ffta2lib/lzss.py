"""FFTA2 LZSS codec (as used by the game for the unit graphics).

Window 4096, max match 18, min match 3.
Header: u32 little endian, low nibble of byte0 == compression type (1),
        upper 24 bits == uncompressed size.

The stream is a sequence of groups: one flag byte followed by up to eight
items.  The bits of the flag byte are read from the most significant one
down; a cleared bit means that the item is a literal byte, a set bit means
that the item is a back reference of two bytes::

    i = ((length - 3) << 4) | ((distance - 1) >> 8)
    j = (distance - 1) & 0xFF

``distance`` is the number of bytes the match is away from the current
position, the copy starts at ``position - distance``.

The encoder of the game is not byte stable: it picks a different match
than the encoder of this module would.  A file that was not changed must
therefore never be compressed again (see the note in ``ffta2lib/codecs.py``).
"""

import struct

#: the size of the sliding window
WINDOW = 4096

#: the shortest match the format can hold
MIN_MATCH = 3

#: the longest match the format can hold
MAX_MATCH = 18


class LZSSError(Exception):
    pass


def decode(src: bytes) -> bytes:
    if len(src) < 4:
        raise LZSSError("too short")
    header = struct.unpack_from("<I", src, 0)[0]
    comp_type = (header >> 4) & 0x0F
    if comp_type != 0x1:
        raise LZSSError("not LZSS compressed (type=%d)" % comp_type)
    size = header >> 8
    if size < 0 or size > 0xFFFFFF:
        raise LZSSError("bad uncompressed size")

    out = bytearray()
    pos = 4
    flag = 0
    flag_index = 7
    src_len = len(src)
    while len(out) < size:
        if flag_index == 7:
            if pos >= src_len:
                break
            flag = src[pos]
            pos += 1
        if not (flag >> flag_index) & 1:
            if pos >= src_len:
                break
            out.append(src[pos])
            pos += 1
        else:
            if pos + 1 >= src_len:
                break
            i = src[pos]
            j = src[pos + 1]
            pos += 2
            offset = ((i & 0x0F) << 8) | j
            num = ((i & 0xF0) >> 4) + 3
            start = len(out) - offset - 1
            if start < 0:
                raise LZSSError("bad back reference")
            for k in range(num):
                out.append(out[start + k])
        flag_index = 7 if flag_index == 0 else flag_index - 1
    return bytes(out)


def is_compressed(data: bytes) -> bool:
    if len(data) < 4:
        return False
    header = struct.unpack_from("<I", data, 0)[0]
    return ((header >> 4) & 0x0F) == 0x1


def header_size(data: bytes, pos: int = 0) -> int:
    """The size of the uncompressed data of the stream at *pos*."""
    if pos + 4 > len(data):
        raise LZSSError("too short")
    header = struct.unpack_from("<I", data, pos)[0]
    if ((header >> 4) & 0x0F) != 0x1:
        raise LZSSError("not LZSS compressed (type=%d)" % ((header >> 4) & 0x0F))
    return header >> 8


def stream_length(data: bytes, pos: int) -> int:
    """The number of bytes the LZSS stream at *pos* occupies.

    The four byte header of the stream is part of the result.
    """
    size = header_size(data, pos)
    p = pos + 4
    out = 0
    flag = 0
    flag_index = 7
    n = len(data)
    while out < size:
        if flag_index == 7:
            if p >= n:
                raise LZSSError("the stream is truncated")
            flag = data[p]
            p += 1
        if not (flag >> flag_index) & 1:
            if p >= n:
                raise LZSSError("the stream is truncated")
            p += 1
            out += 1
        else:
            if p + 1 >= n:
                raise LZSSError("the stream is truncated")
            i = data[p]
            p += 2
            out += ((i & 0xF0) >> 4) + 3
        flag_index = 7 if flag_index == 0 else flag_index - 1
    return p - pos


def _hash(data, pos):
    """The hash of the three bytes at *pos*."""
    return (data[pos] << 10) | (data[pos + 1] << 5) | data[pos + 2]


def encode(data: bytes, max_chain: int = 24) -> bytes:
    """Compress *data* the way the game stores its compressed files.

    The result always decodes back into *data*; it is not the same byte
    sequence the game produces (the game picks a different match when
    several are as long).
    """
    out = bytearray()
    out += struct.pack("<I", (len(data) << 8) | 0x10)
    size = len(data)
    if size == 0:
        return bytes(out)

    # the hash table holds the most recent position of every three byte
    # sequence, "prev" chains the positions that share it
    head = {}
    prev = [-1] * size
    pos = 0
    group = []
    flags = 0
    while pos < size:
        length = 0
        distance = 0
        if pos + MIN_MATCH <= size:
            key = _hash(data, pos)
            cand = head.get(key, -1)
            limit = pos - WINDOW
            chain = 0
            while cand >= 0 and cand > limit and chain < max_chain:
                # how long does the match last?
                n = 0
                while (n < MAX_MATCH and pos + n < size
                       and data[cand + n] == data[pos + n]):
                    n += 1
                if n > length:
                    length = n
                    distance = pos - cand
                    if n == MAX_MATCH:
                        break
                cand = prev[cand]
                chain += 1
        if length >= MIN_MATCH:
            index = ((length - MIN_MATCH) << 4) | ((distance - 1) >> 8)
            group.append(bytes((index, (distance - 1) & 0xFF)))
            flags |= 1 << (7 - (len(group) - 1))
            step = length
        else:
            group.append(bytes((data[pos],)))
            step = 1

        # remember the positions of the bytes that were written
        for k in range(step):
            p = pos + k
            if p + MIN_MATCH <= size:
                key = _hash(data, p)
                prev[p] = head.get(key, -1)
                head[key] = p
        pos += step

        if len(group) == 8:
            out.append(flags)
            out += b"".join(group)
            group = []
            flags = 0
    if group:
        out.append(flags)
        out += b"".join(group)
    return bytes(out)
