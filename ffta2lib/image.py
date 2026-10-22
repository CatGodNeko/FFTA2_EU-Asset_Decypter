"""Minimal PNG reader and writer (no external dependencies).

The writer stores every picture as 8 bit per channel; the reader understands
all the colour types the PNG specification defines for 8 bit images so that
files edited with any other program can be read back.

Colour types
    0  greyscale
    2  truecolour
    3  indexed          (needs a PLTE chunk)
    4  greyscale + alpha
    6  truecolour + alpha

The game uses 16 bit RGB555 palettes with the red channel in the low bits.
"""

import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ writer ---
def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _rgb555_to_rgb8(v):
    """Expand a 5 bit channel to 8 bit (lossless, invertible with >> 3)."""
    return (v << 3) | (v >> 2)


def write_png(path, width, height, pixels, palette=None, alpha=None):
    """Write an 8 bit PNG.

    pixels  : bytes, one byte per pixel, row major (indexed when *palette*
              is given, otherwise luminance/grayscale)
    palette : optional list of (r, g, b) tuples
    alpha   : optional list of alpha values (0-255) matching the palette
    """
    color_type = 3 if palette else 0
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += pixels[y * stride:(y + 1) * stride]

    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    if palette:
        plte = bytearray()
        for (r, g, b) in palette:
            plte += bytes((r, g, b))
        out += _chunk(b"PLTE", bytes(plte))
        if alpha:
            out += _chunk(b"tRNS", bytes(alpha))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(bytes(out))


def write_rgb_png(path, width, height, rgb):
    """Write a truecolour PNG from a bytes-like RGB (3 bytes/pixel) buffer."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(bytes(out))


def png_bytes_rgba(width, height, rgba, palette=None):
    """Build a truecolour + alpha PNG and return its bytes.

    *palette* is optional and only used as a "suggested palette" (a PLTE
    chunk).  Truecolour images are allowed to carry one and it is the easiest
    way to keep the original 16 bit palette of a texture alive across a
    round trip.
    """
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if palette:
        plte = bytearray()
        for (r, g, b) in palette:
            plte += bytes((r, g, b))
        out += _chunk(b"PLTE", bytes(plte))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    return bytes(out)


def write_rgba_png(path, width, height, rgba, palette=None):
    """Write a truecolour + alpha PNG."""
    with open(path, "wb") as f:
        f.write(png_bytes_rgba(width, height, rgba, palette))


def png_bytes_indexed(width, height, indices, palette, transparent=None):
    """Build an indexed (colour type 3) PNG and return its bytes.

    *palette*    list of (r, g, b) tuples, at most 256 entries
    *transparent* index of a fully transparent palette entry (or None)
    """
    if len(palette) > 256:
        raise ValueError("too many palette entries")
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)
        raw += bytes(indices[y * stride:(y + 1) * stride])
    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
    plte = bytearray()
    for (r, g, b) in palette:
        plte += bytes((r, g, b))
    out += _chunk(b"PLTE", bytes(plte))
    if transparent is not None:
        out += _chunk(b"tRNS", bytes([transparent] + [255] * (len(palette) - 1)))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    return bytes(out)


def write_indexed_png(path, width, height, indices, palette, transparent=None):
    """Write an indexed (colour type 3) PNG."""
    with open(path, "wb") as f:
        f.write(png_bytes_indexed(width, height, indices, palette,
                                  transparent))


def bgr555_to_rgb555(pal):
    """Convert 16 bit BGR555 words to a list of (r, g, b, a) with 5 bit
    channels scaled to 8 bit."""
    out = []
    for w in pal:
        r = (w & 0x1F) * 255 // 31
        g = ((w >> 5) & 0x1F) * 255 // 31
        b = ((w >> 10) & 0x1F) * 255 // 31
        out.append((r, g, b))
    return out


def bgr555_bytes_to_rgb(pal):
    """32 byte BGR555 palette -> (palette list, rgba lookup table)."""
    import struct as _s
    words = _s.unpack("<%dH" % (len(pal) // 2), pal)
    return bgr555_to_rgb555(words)


# ------------------------------------------------------------------ reader ---
class PngImage(object):
    """A decoded PNG."""

    def __init__(self, width, height, color_type, bit_depth, palette,
                 transparency, pixels):
        self.width = width
        self.height = height
        self.color_type = color_type
        self.bit_depth = bit_depth
        self.palette = palette              # list of (r, g, b) or None
        self.transparency = transparency    # list of alpha values or None
        self.pixels = pixels                # raw samples, row major

    # -- helpers ---------------------------------------------------------
    def to_rgba(self):
        """Return the picture as RGBA bytes (4 bytes per pixel)."""
        w, h = self.width, self.height
        n = w * h
        px = self.pixels
        out = bytearray(n * 4)
        if self.color_type == 3:
            pal = self.palette or []
            trns = self.transparency or []
            for i in range(n):
                idx = px[i]
                if idx < len(pal):
                    r, g, b = pal[idx]
                else:
                    r = g = b = 0
                a = trns[idx] if idx < len(trns) else 255
                o = i * 4
                out[o] = r
                out[o + 1] = g
                out[o + 2] = b
                out[o + 3] = a
            return bytes(out)
        if self.color_type == 0:
            for i in range(n):
                g = px[i]
                o = i * 4
                out[o] = out[o + 1] = out[o + 2] = g
                out[o + 3] = 255
            return bytes(out)
        if self.color_type == 4:
            for i in range(n):
                g = px[i * 2]
                o = i * 4
                out[o] = out[o + 1] = out[o + 2] = g
                out[o + 3] = px[i * 2 + 1]
            return bytes(out)
        if self.color_type == 2:
            for i in range(n):
                o = i * 4
                out[o] = px[i * 3]
                out[o + 1] = px[i * 3 + 1]
                out[o + 2] = px[i * 3 + 2]
                out[o + 3] = 255
            return bytes(out)
        if self.color_type == 6:
            return bytes(px[:n * 4])
        raise ValueError("unsupported colour type %d" % self.color_type)

    def to_indexed(self, palette):
        """Return (indices, used_palette) for the given RGB555 palette.

        The palette is a list of 16 bit words (red in the low bits).  Every
        pixel is mapped to the closest entry; pixels that are fully
        transparent get the index 0.
        """
        pal_rgb = [(w & 0x1F, (w >> 5) & 0x1F, (w >> 10) & 0x1F)
                   for w in palette]
        n = self.width * self.height
        rgba = self.to_rgba()
        out = bytearray(n)
        cache = {}
        for i in range(n):
            o = i * 4
            a = rgba[o + 3]
            if a == 0:
                out[i] = 0
                continue
            key = (rgba[o], rgba[o + 1], rgba[o + 2])
            idx = cache.get(key)
            if idx is None:
                best = 0
                bestd = None
                for j, (r5, g5, b5) in enumerate(pal_rgb):
                    dr = (key[0] - _rgb555_to_rgb8(r5)) ** 2
                    dg = (key[1] - _rgb555_to_rgb8(g5)) ** 2
                    db = (key[2] - _rgb555_to_rgb8(b5)) ** 2
                    d = dr + dg + db
                    if bestd is None or d < bestd:
                        bestd = d
                        best = j
                idx = best
                cache[key] = idx
            out[i] = idx
        return bytes(out), palette


def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, bpp):
    """Undo the PNG row filters, return the raw samples."""
    stride = width * bpp
    out = bytearray(stride * height)
    pos = 0
    prev = bytearray(stride)
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError("unknown filter type %d" % ft)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def read_png(path):
    """Read a PNG file and return a :class:`PngImage`."""
    with open(path, "rb") as f:
        return read_png_bytes(f.read())


def read_png_bytes(data):
    """Read PNG data and return a :class:`PngImage`."""
    if data[:8] != _SIG:
        raise ValueError("not a PNG")
    pos = 8
    idat = bytearray()
    width = height = 0
    bit_depth = color_type = 0
    interlace = 0
    palette = None
    transparency = None
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack_from(">IIBB", body, 0)
            interlace = body[12]
        elif tag == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body) - 2, 3)]
        elif tag == b"tRNS":
            transparency = list(body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if interlace:
        raise ValueError("interlaced PNGs are not supported")
    if bit_depth != 8:
        raise ValueError("only 8 bit PNGs are supported (got %d)" % bit_depth)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(bytes(idat))
    pixels = _unfilter(raw, width, height, channels)
    return PngImage(width, height, color_type, bit_depth, palette,
                    transparency, pixels)


def read_indexed(path, palette=None):
    """Read a PNG and return (width, height, indices, palette).

    *palette* is the expected 16 bit RGB555 palette (a list of words).  When
    given, the indices of the picture are mapped onto it.
    """
    img = read_png(path)
    if palette is not None:
        indices, _ = img.to_indexed(palette)
        return img.width, img.height, indices, palette
    return img.width, img.height, img.pixels, img.palette
