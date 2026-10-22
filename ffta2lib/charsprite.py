"""The unit graphics of FFTA2 (the "char" containers).

Every unit has two files:

``UnitSst``  the *sprite table*: a binary tree that points at the palette
             of the unit and at its sprite map.
``UnitCg``   the *character graphic*: the compressed pixels of every
             sprite of the unit.

The sprite table file is built like this::

    0x00  the node array: 8 byte nodes
              u16 key        (0x00F0 = palette, 0x00FF = sprite map)
              u16 offset     (the offset of the blob, in 16 byte units)
              i16 left       (the index of the left child)
              i16 right      (the index of the right child)
    ...     the blobs, every one of them at a 16 byte boundary:
              u16 size       (the size of the decoded data)
              u16 kind       (1 = sprite map, 2 = palette, 3 = other data)
              the compressed data (an LZSS stream, its header holds the
              size of the decoded data as well)

The nodes form a binary search tree: the left child holds the bigger keys,
the right child the smaller ones.  The nodes are numbered in the order of
their blobs, so the blobs of a file follow the nodes.

The array of the nodes holds two nodes more than the tree needs: the last
two nodes of the array are not part of the tree.  The first of them holds
the size of the file (in 16 byte units) in its offset field, the second one
is empty.

The decoded sprite map is a list of records::

    u16 pieceOffset   the offset of the piece table inside the blob
    u16 unknown       (the same value in every record of a unit)
    u32 spriteOffset  the offset of the compressed pixels in the cg file

followed by the piece tables themselves: a u32 count and then count records
of {i16 x, i16 y, u16 width, u16 height}.  The pixels of a sprite are
stored one byte per pixel; the pieces are cut out of that stream one after
the other and are pasted into the bounding box of all pieces.

The character graphic file holds the compressed pixels of every sprite of
the unit.  Every sprite starts at a four byte boundary, the space between
two sprites is filled with zeroes.
"""

import struct

from . import lzss

#: the key of the node that holds the palette of a unit
PALETTE_KEY = 0x00F0

#: the key of the node that holds the sprite map of a unit
MAP_KEY = 0x00FF

#: the kind of a blob that holds the sprite map
KIND_MAP = 1

#: the kind of a blob that holds the palette
KIND_PALETTE = 2

#: the kind of a blob that holds any other data
KIND_DATA = 3

#: the alignment of the blobs inside the sprite table
BLOB_ALIGN = 16

#: the alignment of the sprites inside the character graphic file
SPRITE_ALIGN = 4

#: the number of colours of one palette of the game
PALETTE_COLORS = 32

#: the colour the game uses for a transparent pixel
TRANSPARENT = (255, 0, 255)


class CharError(Exception):
    pass


# --------------------------------------------------------------------- sst ---
class UnitSst(object):
    """The sprite table file of a unit."""

    def __init__(self, data):
        self.data = data
        self.nodes = []       # (key, offset, left, right) of the node array
        self.tree = []        # the node indices of the tree, in file order
        self.blobs = {}       # node index -> (size, kind, stream)
        self._parse()

    # -- parsing ---------------------------------------------------------
    def _parse(self):
        d = self.data
        if len(d) < 8:
            raise CharError("the sprite table is too short")
        count = len(d) // 8
        raw = [struct.unpack_from("<HHhh", d, i * 8) for i in range(count)]
        # walk the tree
        seen = set()
        stack = [0]
        while stack:
            i = stack.pop()
            if i in seen or not (0 <= i < count):
                continue
            seen.add(i)
            _key, _off, left, right = raw[i]
            if left >= 0:
                stack.append(left)
            if right >= 0:
                stack.append(right)
        if not seen:
            raise CharError("the sprite table has no tree")
        first = min(raw[i][1] for i in seen)
        # the node array ends where the first blob begins
        self.nodes = raw[:first * 2]
        self.tree = sorted(seen, key=lambda i: raw[i][1])
        for i in self.tree:
            _key, off, _l, _r = raw[i]
            base = off * BLOB_ALIGN
            if base + 4 > len(d):
                raise CharError("the sprite table has a broken blob")
            size, kind = struct.unpack_from("<HH", d, base)
            length = lzss.stream_length(d, base + 4)
            self.blobs[i] = (size, kind, d[base + 4:base + 4 + length])

    # -- access ----------------------------------------------------------
    def find(self, key):
        """The node that holds *key* (or None)."""
        count = len(self.nodes)
        i = 0
        while 0 <= i < count:
            k, off, left, right = self.nodes[i]
            if k == key:
                return i
            i = left if key > k else right
        return None

    def blob(self, node):
        """The (size, kind, stream) of the blob of a node."""
        return self.blobs[node]

    def decode(self, node):
        """The decoded data of the blob of a node."""
        _size, _kind, stream = self.blobs[node]
        return lzss.decode(stream)

    def set_blob(self, node, decoded):
        """Replace the data of a blob and compress it again.

        The compressed data of the game is not byte stable, so a blob that
        did not change keeps the bytes it had.
        """
        size, kind, stream = self.blobs[node]
        if lzss.decode(stream) == decoded:
            return
        self.blobs[node] = (len(decoded), kind, lzss.encode(decoded))

    # -- building --------------------------------------------------------
    def build(self, blobs=None):
        """Build a new sprite table.

        *blobs* maps the node index to the new ``(size, kind, stream)`` of
        that node.  The nodes, their order and the kind of every blob stay
        the same; only the offsets follow the new sizes.
        """
        if blobs is None:
            blobs = self.blobs
        node_area = len(self.nodes) * 8
        out = bytearray(self.data[:node_area])
        for i in self.tree:
            size, kind, stream = blobs.get(i, self.blobs[i])
            struct.pack_into("<H", out, i * 8 + 2, len(out) // BLOB_ALIGN)
            out += struct.pack("<HH", size, kind) + stream
            while len(out) % BLOB_ALIGN:
                out += b"\x00"
        # the last two nodes of the array are not part of the tree: the
        # first one holds the size of the file
        if len(self.nodes) > len(self.tree):
            sentinel = len(self.nodes) - (len(self.nodes) - len(self.tree))
            struct.pack_into("<H", out, sentinel * 8 + 2,
                             len(out) // BLOB_ALIGN)
        return bytes(out)


# --------------------------------------------------------------------- map ---
def parse_map(decoded):
    """The sprite records of a decoded sprite map."""
    if len(decoded) < 2:
        raise CharError("the sprite map is too short")
    count = struct.unpack_from("<H", decoded, 0)[0] >> 3
    out = []
    for i in range(count):
        pos = i * 8
        if pos + 8 > len(decoded):
            break
        piece_off, unknown, sprite_off = struct.unpack_from("<HHI", decoded, pos)
        pieces = []
        if piece_off + 4 <= len(decoded):
            n = struct.unpack_from("<I", decoded, piece_off)[0]
            if n > 4096:
                n = 0
            for j in range(n):
                p = piece_off + 4 + j * 8
                if p + 8 > len(decoded):
                    break
                pieces.append(struct.unpack_from("<HHhh", decoded, p))
        out.append({"pos": pos, "piece_off": piece_off, "unknown": unknown,
                    "sprite_off": sprite_off, "pieces": pieces})
    return out


def build_map(decoded, offsets):
    """A new sprite map with new sprite offsets.

    *offsets* holds the new offset of every sprite; everything else (the
    piece tables, the unknown field) stays as it was.
    """
    out = bytearray(decoded)
    records = parse_map(decoded)
    for rec in records:
        pos = rec["pos"]
        if pos + 8 > len(out):
            break
        struct.pack_into("<I", out, pos + 4, offsets[rec["pos"] // 8])
    return bytes(out)


# ------------------------------------------------------------------ pieces ---
def sprite_size(pieces):
    """The size of the picture of a sprite."""
    if not pieces:
        return 0, 0
    left = min(p[0] for p in pieces)
    top = min(p[1] for p in pieces)
    right = max(p[0] + p[2] for p in pieces)
    bottom = max(p[1] + p[3] for p in pieces)
    return right - left, bottom - top


def compose(pieces, pixels):
    """Cut the pieces out of the pixel stream and compose them.

    Returns ``(pixels, width, height)``; the pixels are the indices of the
    picture, one byte per pixel.
    """
    w, h = sprite_size(pieces)
    if w <= 0 or h <= 0:
        return b"", 0, 0
    left = min(p[0] for p in pieces)
    top = min(p[1] for p in pieces)
    buf = bytearray(w * h)
    pos = 0
    for (x, y, pw, ph) in pieces:
        n = pw * ph
        chunk = pixels[pos:pos + n]
        pos += n
        if len(chunk) < n:
            chunk = chunk + bytes(n - len(chunk))
        for row in range(ph):
            dst = (y - top + row) * w + (x - left)
            src = row * pw
            buf[dst:dst + pw] = chunk[src:src + pw]
    return bytes(buf), w, h


def cut(pieces, w, h, pixels):
    """Cut the pieces out of a picture again.

    *pixels* holds one index per pixel of a picture of the size *w* x *h*;
    the result is the byte stream the game stores.
    """
    left = min(p[0] for p in pieces)
    top = min(p[1] for p in pieces)
    out = bytearray()
    for (x, y, pw, ph) in pieces:
        for row in range(ph):
            src = (y - top + row) * w + (x - left)
            out += pixels[src:src + pw]
    return bytes(out)


# --------------------------------------------------------------- character ---
def split_sprites(cg, records):
    """Split the character graphic file into the streams of the sprites.

    Returns a list of ``(offset, length, data)``; the length is the number
    of bytes the compressed stream occupies.
    """
    out = []
    for rec in records:
        off = rec["sprite_off"]
        if off + 4 > len(cg):
            raise CharError("the sprite map points outside the file")
        length = lzss.stream_length(cg, off)
        out.append((off, length, cg[off:off + length]))
    return out


def build_cg(streams):
    """Build a new character graphic file.

    Every stream starts at a four byte boundary, the space between two
    streams is filled with zeroes.
    """
    out = bytearray()
    offsets = []
    for stream in streams:
        while len(out) % SPRITE_ALIGN:
            out += b"\x00"
        offsets.append(len(out))
        out += stream
    while len(out) % SPRITE_ALIGN:
        out += b"\x00"
    return bytes(out), offsets


# ----------------------------------------------------------------- pixels ---
# A pixel of a sprite is one byte::
#
#     bit 7 .. 5   the shade: a brightness level (0 is the darkest)
#     bit 4 .. 0   the index of the colour in the palette of the unit
#
# The palette of a unit holds 32 colours; a sprite set may hold several
# of those palettes (the game picks the one of the unit that is drawn).
#
# The byte 0 is the empty pixel: the game shows it transparent.  Every
# other byte is a colour: the colour of the palette, darkened by the
# shade.  The shade works like a quarter step: the shade 7 is the colour
# of the palette itself, the shade 5 is 75% of it, the shade 3 is 50%
# and the shade 1 is 25%.  The sprites therefore mix two ways of
# shading: some of them walk along the ramps of the palette (the palette
# holds a ramp of greys, one of orange, one of blue, ...) and use the
# shade for the light of the picture, the others use the shade only.

def palette_groups(pal):
    """Split a palette blob into the palettes of the game.

    Every palette holds 32 colours (the blob of the game always is a
    multiple of 64 bytes).
    """
    words = list(struct.unpack("<%dH" % (len(pal) // 2), pal))
    return [words[i:i + PALETTE_COLORS]
            for i in range(0, len(words), PALETTE_COLORS)]


def word_rgb5(word):
    """A 16 bit RGB555 word (red in the low bits) -> a 5 bit RGB triple."""
    return (word & 0x1F, (word >> 5) & 0x1F, (word >> 10) & 0x1F)


def rgb5_to_rgb8(rgb):
    """A 5 bit RGB triple -> an 8 bit RGB triple."""
    return tuple(c * 255 // 31 for c in rgb)


def shade_rgb5(rgb, shade):
    """Darken a 5 bit RGB triple by a shade level (0 is the darkest)."""
    factor = shade + 1
    return tuple(c * factor // 8 for c in rgb)


def shade_factor(shade):
    """The brightness of a shade level (1/8 .. 8/8)."""
    return (shade + 1) / 8.0


def sprite_table(words):
    """The 256 colour table of a sprite.

    The index of the table is the byte the game stores for a pixel; the
    entry 0 is the transparent colour.
    """
    table = []
    for shade in range(8):
        for colour in range(PALETTE_COLORS):
            rgb = shade_rgb5(word_rgb5(words[colour]), shade)
            table.append(rgb5_to_rgb8(rgb))
    table[0] = TRANSPARENT
    return table
