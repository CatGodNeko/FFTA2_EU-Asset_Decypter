#!/usr/bin/env python3
"""Change an asset, rebuild the ROM and check that the change arrived.

    python3 tools/verify_edit.py

Two things are tried:

* a picture of the menu (a "top sprite"): a pixel is painted into it,
* a sprite of a unit (the "char/rom" graphics): a pixel is painted into it.

In both cases the ROM is built again and the sprite is read back out of
the new ROM to make sure that the game would show the new pixel.
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ffta2lib import codecs, image                       # noqa: E402
from ffta2lib import charsprite, lzss                   # noqa: E402
from ffta2lib import project as proj                    # noqa: E402
from ffta2lib.archive import Archive, ArchiveWriter     # noqa: E402
from ffta2lib.pak import IdxPak, IdxPakWriter           # noqa: E402

ROM = "/data/FFTA2-EU_A6FPGD_00.nds"
WORK = "/tmp/opencode/verify"


def build_rom(archive, work, overrides):
    """Write the new members into the master archive and build the ROM."""
    writer = ArchiveWriter(archive.idx, archive.bin)
    aidx, abin = writer.build(overrides)
    with open(os.path.join(work, "rom", "files", "master", "pc.idx"), "wb") as f:
        f.write(aidx)
    with open(os.path.join(work, "rom", "files", "master", "pc.bin"), "wb") as f:
        f.write(abin)
    rom = os.path.join(work, "patched.nds")
    dsd = proj.find_dsd(None)
    proj.dsd_build(dsd, os.path.join(work, "rom", "config.yaml"), rom)
    print("built %s (%d bytes)" % (rom, os.path.getsize(rom)))
    return rom


def read_back(rom, work, keys):
    """Read the members of the new ROM."""
    out = os.path.join(work, "rom2")
    shutil.rmtree(out, ignore_errors=True)
    proj.dsd_extract(proj.find_dsd(None), rom, out)
    archive = Archive(os.path.join(out, "files", "master", "pc.idx"),
                      os.path.join(out, "files", "master", "pc.bin"))
    return dict((key, archive.member_bytes(key)) for key in keys)


# ------------------------------------------------------------------ menu ---
def test_menu(archive, work):
    container = None
    for cont, _d in proj.container_specs(archive):
        if cont.id == "menu/atl_rom/atl.pak":
            cont.load(archive)
            container = cont
            break
    if container is None:
        print("the menu container was not found")
        return 1

    index, name, data = container.files()[0]
    ctx = container.make_ctx(index, name)
    assets = container.codec.extract(ctx, data)

    # paint a pixel into the first sprite
    key = sorted(k for k in assets if "_s" in k)[0]
    img = image.read_png_bytes(assets[key])
    pixels = bytearray(img.pixels)
    old = pixels[0]
    pixels[0] = (old + 1) % 16
    print("%s: pixel 0 %d -> %d" % (key, old, pixels[0]))
    assets[key] = image.png_bytes_indexed(img.width, img.height, bytes(pixels),
                                          img.palette)

    new = container.codec.rebuild(ctx, data, assets)
    if new == data:
        print("FAIL: the rebuild did not change anything")
        return 1
    print("the rebuilt member differs from the original (%d bytes)"
          % len(new))

    ip = IdxPak(container.data["idx"], container.data["pak"])
    files = list(ip.files)
    files[index] = new
    idx, pak = IdxPakWriter(container.data["idx"], files).build()
    overrides = {
        tuple(int(x) for x in container.members["idx"].split(":")): idx,
        tuple(int(x) for x in container.members["pak"].split(":")): pak,
    }
    rom = build_rom(archive, work, overrides)

    # read the sprite back out of the new ROM
    data2 = read_back(rom, work, [container.members["pak"]])[container.members["pak"]]
    ip2 = IdxPak(container.data["idx"], data2)
    codec = codecs.CODECS["top_sprite"]
    words, sprites, _tail = codec._parse(ip2.files[index])
    pixels = codec._unpack(sprites[0])
    if pixels[0] != (old + 1) % 16:
        print("FAIL: the new ROM does not hold the new pixel (%d)" % pixels[0])
        return 1
    print("OK: the new ROM holds the new pixel")
    return 0


# ------------------------------------------------------------------ unit ---
def test_unit(archive, work):
    container = None
    for cont, _d in proj.container_specs(archive):
        if cont.id == "char/rom":
            cont.load(archive)
            container = cont
            break
    if container is None:
        print("the unit graphics container was not found")
        return 1

    index, name, data = container.files()[0]
    ctx = container.make_ctx(index, name)
    assets = container.codec.extract(ctx, data)

    # paint a pixel into the first sprite of the first unit
    key = sorted(k for k in assets if "_spr" in k)[0]
    img = image.read_png_bytes(assets[key])
    pixels = bytearray(img.pixels)
    spot = next(i for i, v in enumerate(pixels) if v)
    old = pixels[spot]
    pixels[spot] = (old + 1) % 256
    print("%s: pixel %d %d -> %d" % (key, spot, old, pixels[spot]))
    assets[key] = image.png_bytes_indexed(img.width, img.height, bytes(pixels),
                                          img.palette, transparent=0)

    new = container.codec.rebuild(ctx, data, assets)
    if new == data:
        print("FAIL: the rebuild did not change anything")
        return 1
    print("the rebuilt sprite table differs from the original (%d bytes)"
          % len(new[0]))

    sst = IdxPak(container.data["sst_idx"], container.data["sst_pak"])
    cg = IdxPak(container.data["cg_idx"], container.data["cg_pak"])
    sst_files = list(sst.files)
    cg_files = list(cg.files)
    sst_files[index] = new[0]
    cg_files[index] = new[1]
    sst_idx, sst_pak = IdxPakWriter(container.data["sst_idx"], sst_files).build()
    cg_idx, cg_pak = IdxPakWriter(container.data["cg_idx"], cg_files).build()
    overrides = {}
    for role, blob in (("sst_idx", sst_idx), ("sst_pak", sst_pak),
                       ("cg_idx", cg_idx), ("cg_pak", cg_pak)):
        overrides[tuple(int(x) for x in container.members[role].split(":"))] = blob
    rom = build_rom(archive, work, overrides)

    # read the sprite back out of the new ROM
    keys = [container.members["cg_pak"], container.members["sst_pak"]]
    got = read_back(rom, work, keys)
    cg2 = IdxPak(container.data["cg_idx"], got[container.members["cg_pak"]])
    sst2 = IdxPak(container.data["sst_idx"], got[container.members["sst_pak"]])
    table = charsprite.UnitSst(sst2.files[index])
    records = charsprite.parse_map(table.decode(table.find(charsprite.MAP_KEY)))
    off = records[0]["sprite_off"]
    length = lzss.stream_length(cg2.files[index], off)
    data_bytes = lzss.decode(cg2.files[index][off:off + length])
    picture, w, h = charsprite.compose(records[0]["pieces"], data_bytes)
    if picture[spot] != (old + 1) % 256:
        print("FAIL: the new ROM does not hold the new pixel (%d)"
              % picture[spot])
        return 1
    print("OK: the new ROM holds the new pixel")
    return 0


def main():
    dsd = proj.find_dsd(None)
    if not dsd:
        print("no dsd found")
        return 1
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    proj.dsd_extract(dsd, ROM, os.path.join(WORK, "rom"))

    archive = proj.Project(WORK).archive()
    result = test_menu(archive, WORK)
    result |= test_unit(archive, WORK)
    return result


if __name__ == "__main__":
    sys.exit(main())
