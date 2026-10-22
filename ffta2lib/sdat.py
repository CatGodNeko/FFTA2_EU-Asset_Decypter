"""Square/Nitro ``SDAT`` sound archive parsing.

An SDAT file is a container made of four blocks::

    0x00  header
    0x10  u32 nSymbOffset / nSymbSize
    0x18  u32 nInfoOffset / nInfoSize
    0x20  u32 nFatOffset  / nFatSize
    0x28  u32 nFileOffset / nFileSize

    SYMB   symbol table: 8 records (SEQ, SEQARC, BANK, WAVEARC,
                            PLAYER, GROUP, PLAYER2, STRM) of file names
    INFO   per record bookkeeping
    FAT    list of {offset, size} of the contained sound files
    FILE   the sound files themselves (SSAR / SBNK / SWAR / SSEQ / STRM)

Every contained file uses the Nitro standard file header
``<4sHHIHH>`` = magic, 0xFEFF, 0x0100, size, 0x10, numBlocks.

A ``SWAR`` (wave archive) holds a number of ``SWAV`` waves::

    0x38  u32 swavCount
    0x3C  u32 offset[swavCount]        (relative to the SWAR start)

and every SWAV blob starts with a 12 byte info block::

    0x00  u8  waveType   0=PCM8 1=PCM16 2=ADPCM
    0x01  u8  isLooped
    0x02  u16 sampleRate
    0x04  u16 time
    0x06  u16 loopOffset
    0x08  u32 loopLength
    0x0C  sample data

The ADPCM data is 4 bit IMA/DVI with the *high* nibble of every byte
holding the first of the two samples of that byte.
"""

import struct

from . import wav

STD_HEADER = struct.Struct("<4sHHIHH")

WAVE_PCM8 = 0
WAVE_PCM16 = 1
WAVE_ADPCM = 2

SYMB_RECORDS = ("SEQ", "SEQARC", "BANK", "WAVEARC", "PLAYER", "GROUP",
                "PLAYER2", "STRM")


class SdatError(Exception):
    pass


class SdatFile(object):
    """One sound file stored inside an SDAT container."""

    def __init__(self, kind, offset, size, data, name=""):
        self.kind = kind
        self.offset = offset
        self.size = size
        self.data = data
        self.name = name

    def __repr__(self):
        return "<SdatFile %s %s %d bytes>" % (self.kind, self.name,
                                              self.size)


class Sdat(object):
    def __init__(self, data):
        self.data = data
        self.symbols = {}      # record name -> list of file names
        self.files = []        # list of SdatFile
        self._parse()

    # -- parsing -------------------------------------------------------
    def _parse(self):
        d = self.data
        if len(d) < 0x40 or d[0:4] != b"SDAT":
            raise SdatError("not an SDAT file")
        (self.file_size, self.header_size, num_blocks) = struct.unpack_from(
            "<IHH", d, 0x08)
        (self.symb_off, self.symb_size, self.info_off, self.info_size,
         self.fat_off, self.fat_size, self.file_off,
         self.file_size_block) = struct.unpack_from("<8I", d, 0x10)

        self.symbols = self._parse_symb()
        self.files = self._parse_fat()

    def _parse_symb(self):
        d = self.data
        out = {}
        if not self.symb_size or self.symb_off + 8 > len(d):
            return out
        base = self.symb_off
        rec_offsets = struct.unpack_from("<8I", d, base + 8)
        for idx, roff in enumerate(rec_offsets):
            if roff == 0:
                continue
            pos = base + roff
            if pos + 4 > len(d):
                continue
            count = struct.unpack_from("<I", d, pos)[0]
            name = SYMB_RECORDS[idx] if idx < len(SYMB_RECORDS) else str(idx)
            names = []
            if name == "SEQARC":
                # record 1 is special: it is a list of groups, every group
                # holds the name of the archive and the offset of a sub
                # record ({nCount, nEntryOffset[]}) holding its contents.
                for i in range(count):
                    g = pos + 4 + i * 8
                    if g + 8 > len(d):
                        break
                    entry_off, sub_off = struct.unpack_from("<II", d, g)
                    group = self._symb_name(base, entry_off)
                    sub = self._symb_group(base, base + sub_off)
                    if group:
                        names.append("%s/" % group)
                    names.extend(sub)
            else:
                names = self._symb_group(base, pos, count)
            out[name] = names
        return out

    def _symb_name(self, base, off):
        """Read a NUL terminated symbol name stored at base+off."""
        if not off:
            return ""
        d = self.data
        start = base + off
        end = d.find(b"\0", start)
        if end < 0:
            end = start
        return d[start:end].decode("latin-1")

    def _symb_group(self, base, pos, count=None):
        """Read a {nCount, nEntryOffset[]} record of symbol names."""
        d = self.data
        if count is None:
            count = struct.unpack_from("<I", d, pos)[0]
        names = []
        for i in range(count):
            off = struct.unpack_from("<I", d, pos + 4 + i * 4)[0]
            if not off:
                names.append("")
                continue
            start = base + off
            end = d.find(b"\0", start)
            names.append(d[start:end].decode("latin-1"))
        return names

    def _parse_fat(self):
        d = self.data
        files = []
        if not self.fat_size:
            return files
        pos = self.fat_off
        if d[pos:pos + 4] != b"FAT ":
            return files
        count = struct.unpack_from("<I", d, pos + 8)[0]
        for i in range(count):
            rec = pos + 12 + i * 16
            off, size = struct.unpack_from("<II", d, rec)
            kind = d[off:off + 4].decode("latin-1")
            files.append(SdatFile(kind, off, size, d[off:off + size]))
        # assign names from the symbol table
        order = ("SEQARC", "SEQ", "BANK", "WAVEARC", "STRM")
        pools = {}
        for key in order:
            pools[key] = list(self.symbols.get(key, []))
        for f in files:
            key = {"SSAR": "SEQARC", "SSEQ": "SEQ", "SBNK": "BANK",
                   "SWAR": "WAVEARC", "STRM": "STRM"}.get(f.kind)
            if key and pools.get(key):
                f.name = pools[key].pop(0)
        return files


def parse_sdat(data):
    return Sdat(data)


# ----------------------------------------------------------------------
# SWAR / SWAV
# ----------------------------------------------------------------------
class Swav(object):
    """A single wave inside a SWAR archive."""

    def __init__(self, wave_type, is_looped, sample_rate, time, loop_offset,
                 loop_length, data, index=0):
        self.wave_type = wave_type
        self.is_looped = is_looped
        self.sample_rate = sample_rate
        self.time = time
        self.loop_offset = loop_offset
        self.loop_length = loop_length
        self.data = data
        self.index = index

    @property
    def sample_count(self):
        if self.wave_type == WAVE_ADPCM:
            return len(self.data) * 2
        if self.wave_type == WAVE_PCM16:
            return len(self.data) // 2
        return len(self.data)

    def decode(self):
        """Return the wave as a list of 16 bit mono samples."""
        if self.wave_type == WAVE_ADPCM:
            return wav.decode_ima_adpcm(self.data, high_nibble_first=True)
        if self.wave_type == WAVE_PCM16:
            return wav.decode_pcm16(self.data)
        if self.wave_type == WAVE_PCM8:
            return wav.decode_pcm8(self.data)
        return []

    def loop_samples(self):
        """(start, length) of the loop region in samples, or None."""
        if not self.is_looped:
            return None
        # the Nitro wave header stores the loop offset / length in units of
        # 8 ADPCM samples (one 32 bit word of four packed bytes)
        return (self.loop_offset * 8, self.loop_length * 8)

    def info_bytes(self):
        """The 12 byte info block of the wave."""
        return struct.pack("<B?3HI", self.wave_type, self.is_looped,
                           self.sample_rate, self.time, self.loop_offset,
                           self.loop_length)

    @property
    def groups(self):
        """The number of blocks of eight samples the wave holds."""
        return self.sample_count // 8

    def blob(self):
        """The wave as it is stored in the archive."""
        return self.info_bytes() + self.data


def parse_swar(data):
    """Parse a SWAR archive and return the list of waves."""
    waves = []
    if data[0:4] != b"SWAR":
        raise SdatError("not a SWAR file")
    size = struct.unpack_from("<I", data, 8)[0]
    count = struct.unpack_from("<I", data, 0x38)[0]
    offsets = struct.unpack_from("<%dI" % count, data, 0x3C)
    for i, off in enumerate(offsets):
        base = off
        (wtype, looped, rate, time, loop_off,
         loop_len) = struct.unpack_from("<B?3HI", data, base)
        end = (offsets[i + 1] if i + 1 < count else size)
        body = data[base + 12:end]
        waves.append(Swav(wtype, looped, rate, time, loop_off, loop_len,
                          body, i))
    return waves


def parse_sdat_file(data):
    """Parse a standalone Nitro ``SWAV`` file."""
    if data[0:4] != b"SWAV":
        raise SdatError("not a SWAV file")
    size = struct.unpack_from("<I", data, 8)[0]
    (wtype, looped, rate, time, loop_off,
     loop_len) = struct.unpack_from("<B?3HI", data, 0x18)
    body = data[0x24:size]
    return Swav(wtype, looped, rate, time, loop_off, loop_len, body, 0)


# ----------------------------------------------------------------------
# building
# ----------------------------------------------------------------------
def align16(value):
    """Round *value* up to a multiple of 16."""
    return (value + 15) & ~15


def split_stream(data):
    """Split a file into the SDAT archives it holds.

    Most files hold a single archive; the big event archives
    (``event_se.pak`` and ``event_midi.pak``) hold many of them, one
    after the other.  Every archive starts at a 16 byte boundary and
    covers its own bytes only.

    Returns the list of ``(offset, size)`` of the archives.
    """
    out = []
    pos = 0
    end = len(data)
    while pos < end:
        if data[pos:pos + 4] != b"SDAT":
            raise SdatError("expected an SDAT at 0x%X" % pos)
        if pos + 0x40 > end:
            raise SdatError("the SDAT at 0x%X is too short" % pos)
        size = struct.unpack_from("<I", data, pos + 8)[0]
        if size < 0x40 or pos + size > end:
            raise SdatError("the SDAT at 0x%X has a broken size" % pos)
        out.append((pos, size))
        pos += size
    return out


def nitro_set_size(blob, size):
    """Set the size of a Nitro sound file (``SSAR``, ``SBNK``, ...).

    The header of such a file holds its total size and the size of its
    single ``DATA`` block; both have to follow when the file changes.
    """
    out = bytearray(blob)
    struct.pack_into("<I", out, 0x08, size)
    struct.pack_into("<I", out, 0x14, size - 0x10)
    return bytes(out)


def build_swar(original, waves):
    """Build a SWAR archive.

    *waves* is the list of the new waves; every wave is the 12 byte info
    block followed by its samples.  The header of *original* is kept, only
    the size fields and the table of the wave offsets are written again.
    """
    count = len(waves)
    header = bytearray(original[:0x3C])
    struct.pack_into("<I", header, 0x38, count)
    table = bytearray(4 * count)
    body = bytearray()
    pos = 0x3C + 4 * count
    for i, wave in enumerate(waves):
        struct.pack_into("<I", table, 4 * i, pos)
        body += wave
        pos += len(wave)
    struct.pack_into("<I", header, 0x08, pos)
    struct.pack_into("<I", header, 0x14, pos - 0x10)
    return bytes(header) + bytes(table) + bytes(body)


def build_sdat(original, files):
    """Build an SDAT archive.

    *files* holds the new content of the files of the archive in the order
    of its FAT.  The symbol and the info block are copied unchanged, the
    FAT and the file block are written again.

    The files keep their place as long as their size does not change; a
    file that grew or shrank moves the files behind it.  Every file
    starts at a 16 byte boundary and the archive itself is padded so that
    its size is a multiple of 16 as well.
    """
    archive = Sdat(original)
    count = len(files)
    if count != len(archive.files):
        raise SdatError("the number of files may not change")
    if all(new == old.data for new, old in zip(files, archive.files)):
        return original

    fat_off = archive.fat_off
    file_off = archive.file_off

    # the place of every file
    offsets = []
    pos = file_off + 12
    moved = False
    for old, new in zip(archive.files, files):
        if not moved and len(new) == old.size:
            offsets.append(old.offset)
        else:
            offsets.append(align16(pos))
            moved = True
        pos = offsets[-1] + len(new)

    # the archive ends at the next 16 byte boundary
    end = align16(pos)
    block_size = end - file_off

    out = bytearray(original[:0x40])
    struct.pack_into("<I", out, 0x08, end)
    struct.pack_into("<I", out, 0x28, block_size)
    # the symbol and the info block (and the padding between them)
    out += original[0x40:fat_off]

    # the FAT
    fat = bytearray(b"FAT ")
    fat += struct.pack("<II", 0x0C + 16 * count, count)
    for offset, data in zip(offsets, files):
        fat += struct.pack("<II", offset, len(data)) + b"\0" * 8
    out += fat

    # the file block
    block = bytearray(b"FILE")
    block += struct.pack("<IHH", block_size, count, 0)
    pos = file_off + 12
    for offset, data in zip(offsets, files):
        block += b"\0" * (offset - pos)
        block += data
        pos = offset + len(data)
    block += b"\0" * (end - pos)
    out += block
    return bytes(out)
