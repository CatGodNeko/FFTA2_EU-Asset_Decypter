"""WAV helpers: PCM reading and writing, IMA ADPCM coding.

Two flavours of 4-bit IMA/DVI ADPCM appear in FFTA2:

* ``strm/bgm*.bin``  - Square's own streamed music.  Low nibble first,
  one byte of pre-amble (the initial 8-bit predictor) at the start.
* Nitro ``SWAV`` waves inside ``.sdat`` archives.  High nibble first and
  no pre-amble.

Both are handled with the same core routine, only the nibble order and
the number of skipped pre-amble bytes differ.

The encoder produces a stream that the matching decoder turns back into
(the same) samples, but it is *not* byte identical to the stream the game
ships: the codec of the game picks the closest of the sixteen codes with
a slightly different rule.  Because of that a file that was not changed
must never be re-encoded (see the note in ``ffta2lib/codecs.py``).
"""

import struct


# ----------------------------------------------------------------------
# IMA ADPCM (DVI) decoder
# ----------------------------------------------------------------------
IMA_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
    2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
    18500, 20350, 22385, 24623, 27086, 29794, 32767,
)

IMA_INDEX_TABLE = (
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
)


def _make_delta_table():
    """The value every code adds to the sample, for every step index."""
    table = []
    for step in IMA_STEP_TABLE:
        row = []
        for nib in range(16):
            diff = step >> 3
            if nib & 1:
                diff += step >> 2
            if nib & 2:
                diff += step >> 1
            if nib & 4:
                diff += step
            if nib & 8:
                diff = -diff
            row.append(diff)
        table.append(tuple(row))
    return tuple(table)


#: ``IMA_DELTA[index][code]`` is the value the code adds to the sample.
IMA_DELTA = _make_delta_table()


def decode_ima_adpcm(data, high_nibble_first=False, preamble=0,
                     predictor=0, index=0):
    """Decode a raw 4-bit IMA/DVI ADPCM stream into a list of 16 bit samples.

    ``data``          the compressed payload
    ``high_nibble_first``  order of the two samples packed in every byte.
                           Nitro SWAV waves use ``True`` (the first sample of
                           a byte lives in the high nibble), the Square BGM
                           streams use ``False``.
    ``preamble``      number of leading bytes to skip (the Square streams
                       store one byte holding the initial 8-bit predictor).
    """
    out = []
    add = out.append
    pred = predictor
    idx = index
    delta = IMA_DELTA
    itable = IMA_INDEX_TABLE
    if high_nibble_first:
        for byte in data[preamble:]:
            row = delta[idx]
            code = byte >> 4
            v = pred + row[code]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            pred = v
            add(v)
            i = idx + itable[code]
            idx = 0 if i < 0 else (88 if i > 88 else i)
            row = delta[idx]
            code = byte & 0x0F
            v = pred + row[code]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            pred = v
            add(v)
            i = idx + itable[code]
            idx = 0 if i < 0 else (88 if i > 88 else i)
    else:
        for byte in data[preamble:]:
            row = delta[idx]
            code = byte & 0x0F
            v = pred + row[code]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            pred = v
            add(v)
            i = idx + itable[code]
            idx = 0 if i < 0 else (88 if i > 88 else i)
            row = delta[idx]
            code = byte >> 4
            v = pred + row[code]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            pred = v
            add(v)
            i = idx + itable[code]
            idx = 0 if i < 0 else (88 if i > 88 else i)
    return out


def encode_ima_adpcm(samples, high_nibble_first=True):
    """Encode 16 bit samples as a raw 4-bit IMA/DVI ADPCM stream.

    Every code tries all sixteen possibilities and keeps the one that
    reproduces the sample best; this is the rule the game uses as well
    (its streams can be reproduced byte for byte that way).

    The stream is decoded by :func:`decode_ima_adpcm` back into samples
    that are as close to the input as the format allows.  It is *not*
    byte identical to the stream the game ships (the game does not round
    the predictor), so a stream that was not changed has to be kept as it
    is.
    """
    out = bytearray()
    add = out.append
    pred = 0
    idx = 0
    delta = IMA_DELTA
    index_table = IMA_INDEX_TABLE
    high = -1
    for sample in samples:
        if sample > 32767:
            sample = 32767
        elif sample < -32768:
            sample = -32768
        row = delta[idx]
        best_code = 0
        best_err = -1
        for code in range(16):
            value = pred + row[code]
            if value > 32767:
                value = 32767
            elif value < -32768:
                value = -32768
            err = value - sample
            if err < 0:
                err = -err
            if best_err < 0 or err < best_err:
                best_err = err
                best_code = code
        value = pred + row[best_code]
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        pred = value
        i = idx + index_table[best_code]
        idx = 0 if i < 0 else (88 if i > 88 else i)
        if high < 0:
            high = best_code
        else:
            if high_nibble_first:
                add((high << 4) | best_code)
            else:
                add((best_code << 4) | high)
            high = -1
    if high >= 0:
        add((high << 4) if high_nibble_first else high)
    return bytes(out)


def decode_pcm8(data):
    """Unsigned 8 bit PCM -> signed 16 bit."""
    return [b - 128 for b in data]


def decode_pcm16(data):
    """Little endian 16 bit PCM -> list of ints."""
    n = len(data) & ~1
    return list(struct.unpack("<%dh" % (n // 2), data[:n]))


# ----------------------------------------------------------------------
# writers
# ----------------------------------------------------------------------
def _smpl_chunk(sample_rate, loops):
    """A "smpl" chunk that holds the loop points of a wave."""
    period = int(1000000000 / sample_rate) if sample_rate else 0
    body = struct.pack("<IIIIIIIII", 0, 0, period, 60, 0, 0, 0, len(loops), 0)
    for start, end in loops:
        body += struct.pack("<IIIIII", 0, 0, start, end, 0, 0)
    return b"smpl" + struct.pack("<I", len(body)) + body


def wav_bytes(channels_data, sample_rate, bits=16, loops=None):
    """The bytes of a RIFF/WAVE file.

    ``channels_data`` is a list of per channel lists of integer samples.
    ``loops`` is an optional list of (start, end) sample positions that
    is written into a "smpl" chunk.
    """
    nch = max(1, len(channels_data))
    frames = len(channels_data[0]) if channels_data else 0
    inter = bytearray()
    for i in range(frames):
        for c in range(nch):
            v = channels_data[c][i] if c < len(channels_data) else 0
            v = max(-32768, min(32767, int(v)))
            inter += struct.pack("<h", v)
    data = bytes(inter)
    byte_rate = sample_rate * nch * bits // 8
    block_align = nch * bits // 8
    body = b"WAVE"
    body += b"fmt " + struct.pack("<IHHIIHH", 16, 1, nch, sample_rate,
                                  byte_rate, block_align, bits)
    body += b"data" + struct.pack("<I", len(data)) + data
    if loops:
        body += _smpl_chunk(sample_rate, loops)
    # the size of the RIFF chunk covers everything but its own header
    return b"RIFF" + struct.pack("<I", len(body)) + body


def wav_bytes_mono(samples, sample_rate, loops=None):
    """The bytes of a mono 16 bit RIFF/WAVE file."""
    return wav_bytes([samples], sample_rate, loops=loops)


def write_wav(path, channels_data, sample_rate, bits=16):
    """Write a RIFF/WAVE file.

    ``channels_data`` is a list of per channel lists of integer samples.
    """
    with open(path, "wb") as f:
        f.write(wav_bytes(channels_data, sample_rate, bits))


def write_wav_mono(path, samples, sample_rate, bits=16):
    """Convenience wrapper for mono 16 bit PCM."""
    write_wav(path, [samples], sample_rate, bits)


# ----------------------------------------------------------------------
# reader
# ----------------------------------------------------------------------
def _riff_chunks(raw):
    """Yield (id, body) of every chunk of a RIFF file."""
    if len(raw) < 12 or raw[0:4] != b"RIFF":
        raise ValueError("not a RIFF file")
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    end = min(len(raw), riff_size + 8)
    pos = 12
    while pos + 8 <= end:
        cid = raw[pos:pos + 4]
        csize = struct.unpack_from("<I", raw, pos + 4)[0]
        yield cid, raw[pos + 8:pos + 8 + csize]
        pos += 8 + csize + (csize & 1)


def _decode_ima_blocks(data, block_align):
    """Decode a RIFF 'IMA ' ADPCM payload (four byte pre-amble per block)."""
    out = []
    if block_align <= 4:
        return out
    for start in range(0, len(data) - block_align + 1, block_align):
        block = data[start:start + block_align]
        pred = struct.unpack_from("<h", block, 0)[0]
        index = block[2]
        if index > 88:
            index = 88
        out.extend(decode_ima_adpcm(block[4:], high_nibble_first=False,
                                    predictor=pred, index=index))
    return out


def read_wav(raw):
    """Read a RIFF/WAVE file.

    Returns ``(samples, sample_rate, loops)`` where ``samples`` is a list
    of 16 bit integers of the first channel and ``loops`` is the list of
    the (start, end) sample positions of the "smpl" chunk.  PCM (8, 16, 24
    and 32 bit), 32 bit float and IMA ADPCM files are understood.
    """
    fmt = None
    pcm = b""
    loops = []
    for cid, body in _riff_chunks(raw):
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            pcm = body
        elif cid == b"smpl":
            loops = _read_smpl(body)
    if fmt is None or len(fmt) < 16:
        raise ValueError("the WAV file has no format chunk")
    (audio_format, channels, rate, byte_rate,
     block_align, bits) = struct.unpack_from("<HHIIHH", fmt, 0)
    if channels < 1:
        channels = 1
    if audio_format == 0x11:
        samples = _decode_ima_blocks(pcm, block_align)
    elif audio_format == 3 and bits == 32:
        count = len(pcm) // 4
        values = struct.unpack("<%df" % count, pcm[:count * 4])
        samples = [max(-32768, min(32767, int(v * 32767.0)))
                   for v in values]
    elif audio_format in (1, 0xFFFE):
        if bits == 8:
            samples = [(b - 128) << 8 for b in pcm]
        elif bits == 16:
            n = len(pcm) & ~1
            samples = list(struct.unpack("<%dh" % (n // 2), pcm[:n]))
        elif bits == 24:
            count = len(pcm) // 3
            values = []
            for i in range(count):
                o = i * 3
                v = pcm[o] | (pcm[o + 1] << 8) | (pcm[o + 2] << 16)
                if v & 0x800000:
                    v -= 0x1000000
                values.append(v >> 8)
            samples = values
        elif bits == 32:
            n = len(pcm) & ~3
            samples = list(struct.unpack("<%di" % (n // 4), pcm[:n]))
        else:
            raise ValueError("unsupported WAV format (%d bit)" % bits)
    else:
        raise ValueError("unsupported WAV format (0x%X)" % audio_format)
    if channels > 1:
        samples = samples[::channels]
    return samples, rate, loops


def _read_smpl(body):
    """The loop points of a "smpl" chunk."""
    if len(body) < 36:
        return []
    count = struct.unpack_from("<I", body, 28)[0]
    out = []
    for i in range(count):
        pos = 36 + 24 * i
        if pos + 24 > len(body):
            break
        start, end = struct.unpack_from("<II", body, pos + 8)
        out.append((start, end))
    return out


def riff_ima_to_wav(src, dst, sample_rate=22050):
    """Convert a RIFF/'IMA ' container to a standard RIFF/WAVE file.

    Only the container is rewritten; the ADPCM payload is copied verbatim
    so the result stays a valid IMA ADPCM wave.
    """
    with open(src, "rb") as f:
        raw = f.read()
    if len(raw) < 12 or raw[0:4] != b"RIFF":
        return False
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    pos = 12
    chunks = []
    fmt = None
    data = b""
    while pos + 8 <= min(len(raw), riff_size + 8):
        cid = raw[pos:pos + 4]
        csize = struct.unpack_from("<I", raw, pos + 4)[0]
        body = raw[pos + 8:pos + 8 + csize]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        else:
            chunks.append((cid, body))
        pos += 8 + csize + (csize & 1)
    if fmt is None:
        fmt = struct.pack("<HHIIHH", 0x11, 1, sample_rate,
                          sample_rate // 2, 256, 4)
    (audio_format, channels, rate, byte_rate, block_align,
     bits) = struct.unpack("<HHIIHH", fmt[:16])
    if audio_format == 1:
        out_fmt = fmt[:16]
    else:
        out_fmt = struct.pack("<HHIIHH", 0x11, channels, rate, byte_rate,
                              block_align, bits) + fmt[16:]
    body = b"fmt " + struct.pack("<I", len(out_fmt)) + out_fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    for cid, cbody in chunks:
        body += cid + struct.pack("<I", len(cbody)) + cbody
    out = b"RIFF" + struct.pack("<I", len(body)) + b"WAVE"
    with open(dst, "wb") as f:
        f.write(out + body)
    return True
