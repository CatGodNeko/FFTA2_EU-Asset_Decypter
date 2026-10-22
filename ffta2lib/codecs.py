"""Codecs that turn native FFTA2 data into editable files and back.

Every codec implements two methods:

    extract(ctx, data)             -> {asset name: file content}
    rebuild(ctx, data, assets)     -> new native bytes

``assets`` holds the *new* content of the assets the user changed; the
assets that are missing keep the content they had in the original data.
That way a file is only ever rebuilt from data that is actually known.

The native formats are described in the doc strings of the matching
``tools/convert_*.py`` extractors.

All the codecs have to survive a round trip: extracting a file and building
it again has to give back the very same bytes.  ``tools/selftest.py`` checks
that for every file of the game.
"""

import struct

from . import charsprite
from . import image
from . import lzss
from . import sdat
from . import wav

# --------------------------------------------------------------- palettes ---


def words_to_rgb8(words):
    """16 bit RGB555 words (red in the low bits) -> list of (r, g, b)."""
    out = []
    for w in words:
        out.append((
            (w & 0x1F) * 255 // 31,
            ((w >> 5) & 0x1F) * 255 // 31,
            ((w >> 10) & 0x1F) * 255 // 31,
        ))
    return out


def rgb8_to_words(rgb):
    """(r, g, b) 8 bit tuples -> 16 bit RGB555 words."""
    return [((r * 31 + 127) // 255) | (((g * 31 + 127) // 255) << 5)
            | (((b * 31 + 127) // 255) << 10) for (r, g, b) in rgb]


def palette_bytes(words):
    """16 bit RGB555 words -> the little endian byte blob of the game."""
    return b"".join(struct.pack("<H", w & 0xFFFF) for w in words)


def words_from_bytes(data):
    return list(struct.unpack("<%dH" % (len(data) // 2), data))


def words_of(pal_rgb, fallback, size):
    """The palette words for a list of RGB values.

    *fallback* holds the words of the original palette.  When the given
    colours are the 8 bit form of that palette the original words are
    returned unchanged, so that a round trip does not lose a bit the 8 bit
    form cannot hold (the game leaves the top bit of a colour unused, but
    a file that was not touched must not change either way).
    """
    fallback = list(fallback)
    n = min(len(pal_rgb), size)
    if n and list(pal_rgb[:n]) == words_to_rgb8(fallback[:n]):
        words = fallback[:n]
    else:
        words = [((r * 31 + 127) // 255) | (((g * 31 + 127) // 255) << 5)
                 | (((b * 31 + 127) // 255) << 10)
                 for (r, g, b) in pal_rgb[:n]]
    while len(words) < size:
        words.append(fallback[len(words)] if len(words) < len(fallback) else 0)
    return words[:size]


def pick_palette(img, fallback_words, size=None):
    """The palette an edited PNG asks for, or the original one.

    A PNG may carry a PLTE chunk; if it does, that palette is used (the
    user may have changed the colours).  Otherwise the original palette of
    the native data is kept.  The result always has ``size`` entries.
    """
    if size is None:
        size = len(fallback_words)
    if img.palette:
        words = words_of(img.palette, fallback_words, size)
    else:
        words = words_of([], fallback_words, size)
    return words, words_to_rgb8(words)


def palette_png(words):
    """A picture that shows a palette (16 colours per row)."""
    pal = words_to_rgb8(words)
    cols = 16
    rows = max(1, (len(pal) + cols - 1) // cols)
    idx = bytes((i % cols) + cols * (i // cols) for i in range(len(pal)))
    return image.png_bytes_indexed(cols, rows, idx, pal)


def palette_of(img, fallback, size):
    """The palette words of a picture that shows a palette."""
    if img.color_type == 3:
        rgb = img.palette or []
    else:
        rgba = img.to_rgba()
        n = min(size, img.width * img.height)
        rgb = [(rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
               for i in range(n)]
    return words_of(rgb, fallback, size)


def new_palette(original, assets, keys, size):
    """The palette the user wants for a file.

    The assets are looked at in the order of *keys*; the first one that
    carries a palette which differs from the original wins.  When no asset
    was changed the original palette is kept.
    """
    original = list(original[:size])
    for key in keys:
        if key not in assets:
            continue
        img = image.read_png_bytes(assets[key])
        words = palette_of(img, original, size)
        if words != original:
            return words
    return original


def quantize(rgba, palette_rgb, size=None):
    """Map RGBA pixels onto a palette and return the index bytes.

    Fully transparent pixels become index 0.
    """
    if size is None:
        size = len(palette_rgb)
    cache = {}
    out = bytearray(len(rgba) // 4)
    for i in range(len(out)):
        o = i * 4
        if rgba[o + 3] == 0:
            out[i] = 0
            continue
        key = (rgba[o], rgba[o + 1], rgba[o + 2])
        idx = cache.get(key)
        if idx is None:
            best = 0
            bestd = None
            for j in range(min(size, len(palette_rgb))):
                r, g, b = palette_rgb[j]
                d = ((key[0] - r) ** 2 + (key[1] - g) ** 2
                     + (key[2] - b) ** 2)
                if bestd is None or d < bestd:
                    bestd = d
                    best = j
            idx = best
            cache[key] = idx
        out[i] = idx
    return bytes(out)


def png_to_indices(img, palette_words, max_index=255):
    """The index bytes of a PNG for a paletted native format."""
    if img.color_type == 3:
        idx = img.pixels
        if max_index < 255:
            idx = bytes(v if v <= max_index else max_index for v in idx)
        return idx
    return quantize(img.to_rgba(), words_to_rgb8(palette_words), max_index + 1)


def _alpha_to_8(value, alpha_max):
    """The 8 bit alpha of an alpha field of the native format."""
    if not alpha_max:
        return 255
    return (value * 255 + alpha_max // 2) // alpha_max


def _alpha_from_8(alpha, alpha_max):
    """The alpha field of the native format of an 8 bit alpha."""
    if not alpha_max:
        return 0
    return (alpha * alpha_max + 127) // 255


def quantize_keep_alpha(rgba, palette_rgb, size=None):
    """Map RGBA pixels onto a palette, keeping fully transparent pixels.

    Unlike :func:`quantize` a transparent pixel is not forced to the index
    0: the format of the game stores the alpha separately from the colour
    index, so the index has to survive a round trip.
    """
    if size is None:
        size = len(palette_rgb)
    cache = {}
    out = bytearray(len(rgba) // 4)
    for i in range(len(out)):
        o = i * 4
        key = (rgba[o], rgba[o + 1], rgba[o + 2])
        idx = cache.get(key)
        if idx is None:
            best = 0
            bestd = None
            for j in range(min(size, len(palette_rgb))):
                r, g, b = palette_rgb[j]
                d = ((key[0] - r) ** 2 + (key[1] - g) ** 2
                     + (key[2] - b) ** 2)
                if bestd is None or d < bestd:
                    bestd = d
                    best = j
            idx = best
            cache[key] = idx
        out[i] = idx
    return bytes(out)


# ------------------------------------------------------------------ codec ---
class Codec(object):
    """Base class of all codecs."""

    name = "codec"

    def extract(self, ctx, data):
        """Return {asset name: bytes}."""
        raise NotImplementedError

    def rebuild(self, ctx, data, assets):
        """Return the new native bytes."""
        raise NotImplementedError


# ------------------------------------------------------------- top sprite ---
class TopSpriteCodec(Codec):
    """menu/atl_rom/atl.pak -- "TopSprite" files.

    Layout:  32 byte palette (16 RGB555 colours)
              u8  number of sprites
              n * 0x20 * 4 * 5 bytes of sprite data
              the rest of the file (the game does not use it)

    A sprite is built from 20 tiles of 8x8 pixels with 4 bits per pixel
    (low nibble first) that are placed in a 4x5 grid, so 32x40 pixels.

    The sprites are written as indexed PNGs: the palette of the file is
    the PLTE chunk of every picture, so the indices of the game survive a
    round trip even when the palette holds the same colour twice.
    """

    name = "top_sprite"

    TILE = 0x20 * 4 * 5
    W, H = 32, 40
    COLORS = 16

    def _parse(self, data):
        """Return (palette words, [sprite blobs], trailing bytes)."""
        if len(data) < 33:
            return [], [], b""
        words = words_from_bytes(data[:32])
        count = data[32]
        pos = 33
        sprites = []
        for _s in range(count):
            blob = data[pos:pos + self.TILE]
            pos += self.TILE
            if len(blob) < self.TILE:
                break
            sprites.append(blob)
        return words, sprites, data[pos:]

    def _unpack(self, blob):
        pixels = bytearray(self.W * self.H)
        for t in range(20):
            tx = (t % 4) * 8
            ty = (t // 4) * 8
            tile = blob[t * 0x20:(t + 1) * 0x20]
            for p in range(64):
                byte = tile[p >> 1]
                v = (byte & 0xF) if not (p & 1) else (byte >> 4)
                pixels[(ty + (p >> 3)) * self.W + tx + (p & 7)] = v
        return pixels

    def _pack(self, pixels):
        out = bytearray(self.TILE)
        for t in range(20):
            tx = (t % 4) * 8
            ty = (t // 4) * 8
            for p in range(64):
                v = pixels[(ty + (p >> 3)) * self.W + tx + (p & 7)] & 0xF
                if p & 1:
                    out[t * 0x20 + (p >> 1)] |= v << 4
                else:
                    out[t * 0x20 + (p >> 1)] |= v
        return bytes(out)

    def extract(self, ctx, data):
        out = {}
        words, sprites, _tail = self._parse(data)
        pal = words_to_rgb8(words)
        for s, blob in enumerate(sprites):
            pixels = self._unpack(blob)
            out["%s_s%d.png" % (ctx.name, s)] = image.png_bytes_indexed(
                self.W, self.H, pixels, pal)
        out["%s_pal.png" % ctx.name] = palette_png(words)
        return out

    def rebuild(self, ctx, data, assets):
        if not assets:
            return data
        words, sprites, tail = self._parse(data)
        keys = ["%s_pal.png" % ctx.name]
        keys += ["%s_s%d.png" % (ctx.name, s) for s in range(len(sprites))]
        words = new_palette(words, assets, keys, self.COLORS)
        out = []
        for s, blob in enumerate(sprites):
            key = "%s_s%d.png" % (ctx.name, s)
            if key not in assets:
                out.append(blob)
                continue
            img = image.read_png_bytes(assets[key])
            if img.width != self.W or img.height != self.H:
                raise ValueError("%s: a top sprite must keep its size "
                                 "(%dx%d)" % (key, self.W, self.H))
            idx = png_to_indices(img, words, self.COLORS - 1)
            out.append(self._pack(idx))
        return (palette_bytes(words) + bytes([len(sprites)])
                + b"".join(out) + tail)


# --------------------------------------------------------------- unit face ---
class UnitFaceCodec(Codec):
    """menu/face_rom/face.pak -- "UnitFace" files.

    Layout:  u16 unknown
              u16 width, u16 height
              u8  number of pieces
              n * 4 * i16  source rectangles
              n * 4 * i16  target rectangles
              width * height bytes of 8 bit indexed texture
              0x200 bytes of palette (256 RGB555 colours)
              the rest of the file (the game does not use it)

    The texture is written as an indexed PNG, so the indices of the game
    survive a round trip even when the palette holds the same colour twice.
    """

    name = "unit_face"
    COLORS = 256

    def parse(self, data):
        if len(data) < 8:
            return None
        w, h = struct.unpack_from("<HH", data, 2)
        n = data[7]
        pos = 8
        if pos + 16 * n > len(data):
            return None
        src = [struct.unpack_from("<4h", data, pos + 8 * i) for i in range(n)]
        pos += 8 * n
        tgt = [struct.unpack_from("<4h", data, pos + 8 * i) for i in range(n)]
        pos += 8 * n
        return w, h, n, src, tgt, pos

    def extract(self, ctx, data):
        out = {}
        parsed = self.parse(data)
        if not parsed:
            return out
        w, h, n, src, tgt, pos = parsed
        if w <= 0 or h <= 0:
            return out
        texture = data[pos:pos + w * h]
        if len(texture) < w * h:
            return out
        words = words_from_bytes(data[pos + w * h:pos + w * h + 0x200])
        words += [0] * (self.COLORS - len(words))
        pal = words_to_rgb8(words)
        out["%s_tex.png" % ctx.name] = image.png_bytes_indexed(
            w, h, texture[:w * h], pal)
        out["%s_pal.png" % ctx.name] = palette_png(words)
        return out

    def rebuild(self, ctx, data, assets):
        key = "%s_tex.png" % ctx.name
        if key not in assets:
            return data
        parsed = self.parse(data)
        if not parsed:
            return data
        w, h, n, src, tgt, pos = parsed
        words = words_from_bytes(data[pos + w * h:pos + w * h + 0x200])
        words += [0] * (self.COLORS - len(words))
        words = new_palette(words, assets,
                            ["%s_pal.png" % ctx.name, key], self.COLORS)
        img = image.read_png_bytes(assets[key])
        if img.width != w or img.height != h:
            raise ValueError("%s: the face texture must keep its size "
                             "(%dx%d)" % (key, w, h))
        idx = png_to_indices(img, words, self.COLORS - 1)
        return (data[:pos] + bytes(idx) + palette_bytes(words)
                + data[pos + w * h + 0x200:])


# -------------------------------------------------------------- map texture ---
class MapTextureCodec(Codec):
    """map/rom/TexData.dat -- the 8 bit indexed texture of a battle map.

    The texture is 256 pixels wide, its height is the size divided by 256.
    The palette lives in another file (PlttData.dat) and is therefore not
    part of the member; the picture is written as an indexed PNG so that
    the indices survive a round trip untouched.
    """

    name = "map_texture"
    WIDTH = 256

    def extract(self, ctx, data):
        out = {}
        if not data:
            return out
        w = self.WIDTH
        h = len(data) // w
        if h <= 0:
            return out
        words = self._palette(ctx)
        pal = words_to_rgb8(words)
        out["%s_tex.png" % ctx.name] = image.png_bytes_indexed(
            w, h, data[:w * h], pal)
        return out

    @staticmethod
    def _palette(ctx):
        """The palette the texture is shown with."""
        pals = ctx.extra.get("palettes") or []
        if pals:
            return pals[0]
        return ctx.extra.get("palette") or [0] * 256

    def rebuild(self, ctx, data, assets):
        key = "%s_tex.png" % ctx.name
        if key not in assets:
            return data
        img = image.read_png_bytes(assets[key])
        if img.width != self.WIDTH:
            raise ValueError("%s: map textures must stay %d pixels wide"
                             % (key, self.WIDTH))
        if img.color_type == 3:
            idx = img.pixels
        else:
            words = self._palette(ctx)
            idx = quantize(img.to_rgba(), words_to_rgb8(words))
        return bytes(idx)


# ------------------------------------------------------------- map palette ---
class MapPaletteCodec(Codec):
    """map/rom/PlttData.dat -- one 512 byte palette (256 RGB555 colours)."""

    name = "map_palette"
    COLORS = 256

    def extract(self, ctx, data):
        out = {}
        if len(data) < 2:
            return out
        words = words_from_bytes(data[:self.COLORS * 2])
        words += [0] * (self.COLORS - len(words))
        pal = words_to_rgb8(words)
        out["%s.png" % ctx.name] = image.png_bytes_indexed(
            16, 16, bytes(range(self.COLORS)), pal)
        return out

    def rebuild(self, ctx, data, assets):
        key = "%s.png" % ctx.name
        if key not in assets:
            return data
        img = image.read_png_bytes(assets[key])
        words, pal = pick_palette(img, words_from_bytes(data[:512]),
                                  self.COLORS)
        return palette_bytes(words) + data[512:]


# ------------------------------------------------------------ efx textures ---
class EfxTextureCodec(Codec):
    """effect/**/*.efx -- the 2D textures of an effect file.

    The file starts with "EFX0010\\0", a float and the total size, followed
    by a list of chunks.  A chunk of type 0xB00 holds a resource; the 'R'
    resources are 2D textures:

        u32 payload size, u16 flags, u8 'R', u8 index, u8 colour mode,
        u8 bits per pixel, u8 unused, u16 width, u16 height,
        u16 hardware width, u16 hardware height, u32 unused,
        palette, then the pixels

    For the colour modes 6 and 7 the pixel byte holds the palette index in
    its low bits and the alpha in the high bits.  The picture is written as
    a truecolour PNG with an alpha channel; a pixel that was not changed
    keeps the byte it had, so a palette with the same colour twice does not
    change the file.
    """

    name = "efx_texture"
    MAGIC = b"EFX0010\x00"

    @staticmethod
    def palette_bytes(colour_mode, bpp):
        if colour_mode in (0, 0x80):
            return bpp << 6
        if colour_mode in (1, 0x81):
            return bpp << 10
        if colour_mode in (6, 7, 8):
            return bpp << 1
        return 0

    @staticmethod
    def pixel_bytes(colour_mode, w, h):
        n = w * h
        if colour_mode == 0:
            return n // 2
        if colour_mode in (1, 6, 7):
            return n
        if colour_mode == 2:
            return n * 2
        if colour_mode == 3:
            return n * 3
        if colour_mode in (4, 0x80, 0x81):
            return n * 4
        if colour_mode == 8:
            return n // 4
        return 0

    def _textures(self, data):
        """Yield (chunk offset, chunk size, description) of every texture."""
        if data[:8] != self.MAGIC:
            return
        pos = 0x10
        n = len(data)
        while pos + 8 <= n:
            size = struct.unpack_from("<I", data, pos)[0]
            flag = struct.unpack_from("<H", data, pos + 4)[0]
            typ = struct.unpack_from("<H", data, pos + 6)[0]
            if size < 8 or pos + size > n:
                break
            if flag == 0x0100 and typ == 0xB00:
                body = data[pos + 8:pos + size]
                if len(body) >= 40 and body[6] == 0x52:
                    cm = body[8]
                    bpp = body[10]
                    w = struct.unpack_from("<H", body, 12)[0]
                    h = struct.unpack_from("<H", body, 14)[0]
                    if w and h and w <= 2048 and h <= 2048:
                        pb = self.palette_bytes(cm, bpp)
                        nb = self.pixel_bytes(cm, w, h)
                        if nb and 24 + pb + nb <= len(body):
                            yield pos, size, dict(
                                colour_mode=cm, bpp=bpp, width=w, height=h,
                                palette=body[24:24 + pb],
                                pixels=body[24 + pb:24 + pb + nb],
                                prefix=body[:24], suffix=body[24 + pb + nb:])
            pos += size

    def _render(self, tex):
        w, h = tex["width"], tex["height"]
        cm = tex["colour_mode"]
        px = tex["pixels"]
        pal = words_to_rgb8(words_from_bytes(tex["palette"]))
        n = w * h
        rgba = bytearray(n * 4)
        if cm in (6, 7, 8):
            entries = max(1, len(pal))
            idx_bits = max(1, entries.bit_length() - 1)
            idx_mask = (1 << idx_bits) - 1
            alpha_max = (1 << (8 - idx_bits)) - 1
            for i in range(n):
                v = px[i]
                c = pal[v & idx_mask] if (v & idx_mask) < len(pal) else (0, 0, 0)
                a = _alpha_to_8(v >> idx_bits, alpha_max)
                o = i * 4
                rgba[o] = c[0]
                rgba[o + 1] = c[1]
                rgba[o + 2] = c[2]
                rgba[o + 3] = a
        elif cm == 0:
            for i in range(n):
                byte = px[i >> 1]
                idx = (byte >> 4) if not (i & 1) else (byte & 0x0F)
                c = pal[idx] if idx < len(pal) else (0, 0, 0)
                o = i * 4
                rgba[o] = c[0]
                rgba[o + 1] = c[1]
                rgba[o + 2] = c[2]
                rgba[o + 3] = 255
        else:
            return None
        return rgba

    def extract(self, ctx, data):
        out = {}
        for i, (_off, _size, tex) in enumerate(self._textures(data)):
            rgba = self._render(tex)
            if rgba is None:
                continue
            pal = words_to_rgb8(words_from_bytes(tex["palette"]))
            out["%s_%d.png" % (ctx.name, i)] = image.png_bytes_rgba(
                tex["width"], tex["height"], rgba, palette=pal)
        return out

    def rebuild(self, ctx, data, assets):
        if not assets:
            return data
        out = bytearray(data)
        for i, (off, size, tex) in enumerate(self._textures(data)):
            key = "%s_%d.png" % (ctx.name, i)
            if key not in assets:
                continue
            body = self._build_body(tex, assets[key])
            if body is None:
                continue
            if len(body) != size - 8:
                raise ValueError("%s: the texture changed its size" % key)
            out[off + 8:off + size] = body
        return bytes(out)

    def _build_body(self, tex, png_bytes):
        img = image.read_png_bytes(png_bytes)
        w, h = img.width, img.height
        if w != tex["width"] or h != tex["height"]:
            raise ValueError("effect textures must keep their size "
                             "(%dx%d)" % (tex["width"], tex["height"]))
        cm = tex["colour_mode"]
        if cm not in (6, 7):
            return None
        old = words_from_bytes(tex["palette"])
        entries = len(old)
        if not entries:
            return None
        idx_bits = max(1, entries.bit_length() - 1)
        idx_mask = (1 << idx_bits) - 1
        alpha_max = (1 << (8 - idx_bits)) - 1
        pal = words_to_rgb8(old)
        rgba = img.to_rgba()
        q = quantize_keep_alpha(rgba, pal, entries)
        px = bytearray(tex["pixels"])
        for i in range(w * h):
            o = i * 4
            old_byte = px[i]
            old_index = old_byte & idx_mask
            old_alpha = old_byte >> idx_bits
            if (old_index < len(pal) and pal[old_index] == (rgba[o], rgba[o + 1],
                                                           rgba[o + 2])
                    and _alpha_to_8(old_alpha, alpha_max) == rgba[o + 3]):
                continue
            a = _alpha_from_8(rgba[o + 3], alpha_max)
            px[i] = (q[i] & idx_mask) | ((a & alpha_max) << idx_bits)
        return (tex["prefix"] + palette_bytes(old) + bytes(px)
                + tex["suffix"])


# ------------------------------------------------------------------ sound ---
class SdatCodec(Codec):
    """sound/**, battle/**, worldmap/** -- the sound archives of the game.

    An archive (``SDAT``) holds three kinds of files: the sequence
    archive (``SSAR``), the sound bank (``SBNK``) and the wave archive
    (``SWAR``).  Every wave of the wave archive is written as a WAV file,
    the other two files are written as they are.

    Some members hold more than one archive (the big event files); the
    assets of those get the number of the archive in their name.

    The samples of a wave are 4 bit IMA ADPCM.  The encoder of the game
    is not byte stable, so a wave whose samples did not change keeps the
    bytes it had.
    """

    name = "sdat"

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _prefixes(ctx, count):
        if count == 1:
            return [ctx.name]
        return ["%s_%d" % (ctx.name, i) for i in range(count)]

    @staticmethod
    def _wave_wav(wave):
        """The WAV file of a wave."""
        samples = wave.decode()
        loops = []
        if wave.is_looped:
            start, length = wave.loop_samples()
            loops = [(start, start + length)]
        return wav.wav_bytes_mono(samples, wave.sample_rate, loops=loops)

    @staticmethod
    def _wave_body(wave, samples):
        """The samples of a wave in the format of the game."""
        if wave.wave_type == sdat.WAVE_ADPCM:
            return wav.encode_ima_adpcm(samples, high_nibble_first=True)
        if wave.wave_type == sdat.WAVE_PCM16:
            out = bytearray()
            for value in samples:
                out += struct.pack("<h", max(-32768, min(32767, value)))
            return bytes(out)
        if wave.wave_type == sdat.WAVE_PCM8:
            return bytes((max(-128, min(127, v)) + 128) & 0xFF
                         for v in samples)
        raise ValueError("unknown wave type %d" % wave.wave_type)

    @staticmethod
    def _sample_count(wave, body):
        if wave.wave_type == sdat.WAVE_ADPCM:
            return len(body) * 2
        if wave.wave_type == sdat.WAVE_PCM16:
            return len(body) // 2
        return len(body)

    def _rebuild_wave(self, wave, wav_bytes):
        """The new bytes of a single wave."""
        samples, _rate, loops = wav.read_wav(wav_bytes)
        if samples == wave.decode():
            # the wave was not changed: keep the bytes of the game
            return wave.blob()
        body = self._wave_body(wave, samples)
        groups = self._sample_count(wave, body) // 8
        if wave.is_looped:
            start = wave.loop_offset
            if loops:
                start = loops[0][0] // 8
            if start >= groups:
                start = max(0, groups - 1)
        else:
            # the game stores 1 and the rest for a wave that does not loop
            start = 1 if groups else 0
        length = max(0, groups - start)
        info = struct.pack("<B?3HI", wave.wave_type, wave.is_looped,
                           wave.sample_rate, wave.time, start, length)
        return info + body

    # -- the codec -------------------------------------------------------
    def extract(self, ctx, data):
        out = {}
        chunks = sdat.split_stream(data)
        prefixes = self._prefixes(ctx, len(chunks))
        for (base, size), prefix in zip(chunks, prefixes):
            blob = data[base:base + size]
            archive = sdat.Sdat(blob)
            for f in archive.files:
                if f.kind == "SWAR":
                    for wave in sdat.parse_swar(f.data):
                        out["%s_w%d.wav" % (prefix, wave.index)] = \
                            self._wave_wav(wave)
                else:
                    out["%s_%s.bin" % (prefix, f.kind)] = f.data
        return out

    def rebuild(self, ctx, data, assets):
        if not assets:
            return data
        chunks = sdat.split_stream(data)
        prefixes = self._prefixes(ctx, len(chunks))
        out = bytearray()
        for (base, size), prefix in zip(chunks, prefixes):
            blob = data[base:base + size]
            archive = sdat.Sdat(blob)
            files = []
            for f in archive.files:
                if f.kind == "SWAR":
                    files.append(self._rebuild_wave_archive(f, assets,
                                                            prefix))
                else:
                    key = "%s_%s.bin" % (prefix, f.kind)
                    files.append(assets.get(key, f.data))
            out += sdat.build_sdat(blob, files)
        return bytes(out)

    def _rebuild_wave_archive(self, f, assets, prefix):
        """The new bytes of the wave archive of an SDAT."""
        waves = sdat.parse_swar(f.data)
        blobs = []
        for wave in waves:
            key = "%s_w%d.wav" % (prefix, wave.index)
            if key in assets:
                blobs.append(self._rebuild_wave(wave, assets[key]))
            else:
                blobs.append(wave.blob())
        if all(b == w.blob() for b, w in zip(blobs, waves)):
            return f.data
        return sdat.build_swar(f.data, blobs)


# ------------------------------------------------------------ unit sprite ---
class CharSpriteCodec(Codec):
    """char/rom -- the graphics of the units of the game.

    Two files belong together: the *sprite table* (``UnitSst``) holds the
    palette and the map of the sprites, the *character graphic*
    (``UnitCg``) holds the pixels of the sprites.  One pair of files is
    one *sprite set*; a sprite set is used by one or more units (the
    units that share the artwork have their own palette).

    The assets of a sprite set are

        <set>_pal00.png ...   the palettes of the set, 32 colours each
        <set>_spr000.png ...  the sprites of the set

    A sprite is written as an indexed PNG: the index of a pixel is the
    byte the game stores (the high three bits are the shade, the low five
    bits the colour), the palette of the picture holds the colour of
    every one of the 256 possible bytes.  A sprite that is not changed
    therefore keeps its bytes exactly; a picture that is stored as
    truecolour is quantised against the same table.

    The game draws a unit with one of the palettes of the set; which one
    depends on the unit and can not be found in the data.  The pictures
    are therefore made with the first palette of the set and every
    palette is written as a picture of its own so that it can be changed.
    """

    name = "char_sprite"

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _palettes(sst):
        """The palettes of a sprite table."""
        node = sst.find(charsprite.PALETTE_KEY)
        if node is None:
            return []
        return charsprite.palette_groups(sst.decode(node))

    @staticmethod
    def _records(sst):
        """The sprite records of a sprite table."""
        node = sst.find(charsprite.MAP_KEY)
        if node is None:
            return []
        return charsprite.parse_map(sst.decode(node))

    @staticmethod
    def _table(groups):
        """The colour table of the sprites (the first palette of the set)."""
        if not groups:
            return [charsprite.TRANSPARENT] * 256
        return charsprite.sprite_table(groups[0])

    @staticmethod
    def _picture(cg, record):
        """The pixels of a sprite: (picture, width, height)."""
        off = record["sprite_off"]
        length = lzss.stream_length(cg, off)
        data = lzss.decode(cg[off:off + length])
        return charsprite.compose(record["pieces"], data)

    @staticmethod
    def _quantise(img, table, w, h):
        """The bytes of a picture that is not indexed."""
        rgba = img.to_rgba()
        out = bytearray(w * h)
        cache = {}
        for i in range(w * h):
            o = i * 4
            if rgba[o + 3] == 0:
                out[i] = 0
                continue
            key = (rgba[o], rgba[o + 1], rgba[o + 2])
            idx = cache.get(key)
            if idx is None:
                best = 1
                bestd = None
                for b in range(1, len(table)):
                    c = table[b]
                    d = ((key[0] - c[0]) ** 2 + (key[1] - c[1]) ** 2
                         + (key[2] - c[2]) ** 2)
                    if bestd is None or d < bestd:
                        bestd = d
                        best = b
                idx = best
                cache[key] = idx
            out[i] = idx
        return bytes(out)

    # -- the codec -------------------------------------------------------
    def extract(self, ctx, data):
        sst_bytes, cg_bytes = data
        sst = charsprite.UnitSst(sst_bytes)
        out = {}
        groups = self._palettes(sst)
        for i, words in enumerate(groups):
            out["%s_pal%02d.png" % (ctx.name, i)] = palette_png(words)
        table = self._table(groups)
        for i, record in enumerate(self._records(sst)):
            try:
                pixels, w, h = self._picture(cg_bytes, record)
            except (charsprite.CharError, lzss.LZSSError):
                continue
            if w <= 0 or h <= 0:
                continue
            out["%s_spr%03d.png" % (ctx.name, i)] = image.png_bytes_indexed(
                w, h, pixels, table, transparent=0)
        return out

    def rebuild(self, ctx, data, assets):
        sst_bytes, cg_bytes = data
        sst = charsprite.UnitSst(sst_bytes)

        # -- the palette of the set
        groups = self._palettes(sst)
        node = sst.find(charsprite.PALETTE_KEY)
        if node is not None and groups:
            new = []
            for i, words in enumerate(groups):
                key = "%s_pal%02d.png" % (ctx.name, i)
                if key in assets:
                    img = image.read_png_bytes(assets[key])
                    words = palette_of(img, words, len(words))
                new.append(words)
            sst.set_blob(node, b"".join(palette_bytes(g) for g in new))
            groups = new

        # -- the sprites
        records = self._records(sst)
        table = self._table(groups)
        streams = charsprite.split_sprites(cg_bytes, records)
        changed = False
        for i, record in enumerate(records):
            key = "%s_spr%03d.png" % (ctx.name, i)
            if key not in assets:
                continue
            img = image.read_png_bytes(assets[key])
            w, h = charsprite.sprite_size(record["pieces"])
            if img.width != w or img.height != h:
                raise ValueError("%s: a sprite must keep its size (%dx%d)"
                                 % (key, w, h))
            if img.color_type == 3:
                picture = img.pixels[:w * h]
            else:
                picture = self._quantise(img, table, w, h)
            data_bytes = charsprite.cut(record["pieces"], w, h, picture)
            off, length, old = streams[i]
            if lzss.decode(old) == data_bytes:
                # the sprite was not changed: keep the bytes of the game
                continue
            streams[i] = (off, length, lzss.encode(data_bytes))
            changed = True

        if not changed:
            return sst.build(), cg_bytes

        # -- the new character graphic file and the new sprite map
        new_cg, offsets = charsprite.build_cg([s[2] for s in streams])
        map_node = sst.find(charsprite.MAP_KEY)
        if map_node is not None:
            sst.set_blob(map_node,
                         charsprite.build_map(sst.decode(map_node), offsets))
        return sst.build(), new_cg


# ---------------------------------------------------------------- registry ---
CODECS = {}
for _c in (TopSpriteCodec(), UnitFaceCodec(), MapTextureCodec(),
           MapPaletteCodec(), EfxTextureCodec(), SdatCodec(),
           CharSpriteCodec()):
    CODECS[_c.name] = _c
