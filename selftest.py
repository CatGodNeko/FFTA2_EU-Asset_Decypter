#!/usr/bin/env python3
"""Check that every codec survives a round trip.

For every container of the game the assets are decoded and encoded again;
the result has to be byte identical to the original data.  A codec that
fails this test cannot be used to rebuild a ROM.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ffta2lib import codecs                                   # noqa: E402
from ffta2lib import project as proj                         # noqa: E402
from ffta2lib.pak import IdxPak                              # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffta2tool import map_palettes                           # noqa: E402


def main():
    rom = sys.argv[1] if len(sys.argv) > 1 else "/data/FFTA2-EU_A6FPGD_00.nds"
    work = sys.argv[2] if len(sys.argv) > 2 else "/tmp/opencode/selftest"

    dsd = proj.find_dsd(None)
    if not dsd:
        print("no dsd found")
        return 1
    proj.dsd_extract(dsd, rom, os.path.join(work, "rom"))
    archive = proj.Project(work).archive()

    palettes = map_palettes(archive)
    failures = 0
    checked = 0
    for container, _desc in proj.container_specs(archive):
        try:
            container.load(archive)
        except KeyError as exc:
            print("SKIP %s (%s)" % (container.id, exc))
            continue
        if container.id == "map/rom/TexData.dat":
            container.extra_context = dict(
                (i, {"palettes": p}) for i, p in palettes.items())
        for index, name, data in container.files():
            ctx = container.make_ctx(index, name)
            try:
                assets = container.codec.extract(ctx, data)
            except Exception as exc:                        # noqa: BLE001
                print("FAIL extract %s[%d]: %s" % (container.id, index, exc))
                failures += 1
                continue
            if not assets:
                continue
            checked += 1
            try:
                back = container.codec.rebuild(ctx, data, assets)
            except Exception as exc:                        # noqa: BLE001
                print("FAIL rebuild %s[%d]: %s" % (container.id, index, exc))
                failures += 1
                continue
            if back != data:
                print("FAIL %s[%d] %s: %d bytes -> %d bytes"
                      % (container.id, index, name, len(data), len(back)))
                failures += 1
    print("checked %d files, %d failures" % (checked, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
