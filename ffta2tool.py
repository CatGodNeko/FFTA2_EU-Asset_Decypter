#!/usr/bin/env python3
"""ffta2tool - extract FFTA2 assets into editable files and rebuild the ROM.

    ffta2tool extract --rom FFTA2-EU_A6FPGD_00.nds --out project
    ffta2tool status  --project project
    ffta2tool build   --project project --rom patched.nds

``extract`` creates a project: a dsd project with the untouched game data,
the decoded assets (PNG and WAV) and a ``project.json`` that describes
every one of them.  Edit the files in ``assets/`` and run ``build`` to get
a new ROM that contains your changes.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ffta2lib import codecs                                   # noqa: E402
from ffta2lib import project as proj                         # noqa: E402
from ffta2lib.archive import Archive, ArchiveWriter          # noqa: E402
from ffta2lib.pak import IdxPak                              # noqa: E402

MAP_TEX = "map/rom/TexData.dat"
MAP_PLTT = "map/rom/PlttData.dat"
MAP_CTRL = "map/rom/MapCtrl.bin"


# ------------------------------------------------------------------ help ---
def _member(members, path):
    key = proj.MEMBERS.get(path)
    if key is None:
        return None
    return archive_bytes(members, key)


def archive_bytes(archive, key):
    return archive.member_bytes(key)


def map_palettes(archive):
    """{texture file index: [palette, ...]} of the battle maps.

    The palettes live in their own container and are handed out to the maps
    in the order the map control table describes.
    """
    out = {}
    try:
        tex_idx = archive.member_bytes(proj.MEMBERS["map/rom/TexData.idx"])
        tex_dat = archive.member_bytes(proj.MEMBERS["map/rom/TexData.dat"])
        plt_idx = archive.member_bytes(proj.MEMBERS["map/rom/PlttData.idx"])
        plt_dat = archive.member_bytes(proj.MEMBERS["map/rom/PlttData.dat"])
        ctrl = archive.member_bytes(proj.MEMBERS[MAP_CTRL])
    except KeyError:
        return out
    tex = IdxPak(tex_idx, tex_dat)
    plt = IdxPak(plt_idx, plt_dat)
    cur = 0
    for i, data in enumerate(tex.files):
        if data is None:
            continue
        pos = 20 + i * 0x14
        if pos + 4 > len(ctrl):
            break
        pals = []
        for _ in range(ctrl[pos + 3]):
            if cur >= len(plt.files):
                break
            pal = plt.files[cur]
            cur += 1
            if pal:
                pals.append(codecs.words_from_bytes(pal[:512]))
        out[i] = pals
    return out


# --------------------------------------------------------------- extract ---
def cmd_extract(args):
    dsd = proj.find_dsd(args.dsd)
    if not dsd:
        print("error: the dsd executable was not found (use --dsd)",
              file=sys.stderr)
        return 1
    out = os.path.abspath(args.out)
    if os.path.isdir(out) and os.listdir(out) and not args.force:
        print("error: %s is not empty (use --force)" % out, file=sys.stderr)
        return 1
    os.makedirs(out, exist_ok=True)

    print("extracting the dsd project ...")
    proj.dsd_extract(dsd, args.rom, os.path.join(out, "rom"))

    prj = proj.Project(out)
    archive = prj.archive()

    containers = []
    descriptions = []
    for container, desc in proj.container_specs(archive):
        try:
            container.load(archive)
        except KeyError as exc:
            print("  skipping %s (%s)" % (container.id, exc))
            continue
        containers.append(container)
        descriptions.append(desc)

    # the map textures need the palettes of the other container
    palettes = map_palettes(archive)
    for container in containers:
        if container.id == MAP_TEX:
            container.extra_context = dict(
                (i, {"palettes": pals}) for i, pals in palettes.items())

    print("decoding the assets ...")
    assets_root = prj.assets_dir
    total = 0
    for container, desc in zip(containers, descriptions):
        files = []
        for index, name, data in container.files():
            ctx = container.make_ctx(index, name)
            try:
                assets = container.codec.extract(ctx, data)
            except Exception as exc:                       # noqa: BLE001
                print("  %s[%d]: %s" % (container.id, index, exc))
                continue
            if not assets:
                continue
            directory = os.path.join(assets_root,
                                     container.file_dir(index, name))
            os.makedirs(directory, exist_ok=True)
            records = []
            for asset_name in sorted(assets):
                content = assets[asset_name]
                path = os.path.join(directory, asset_name)
                with open(path, "wb") as f:
                    f.write(content)
                records.append({
                    "name": asset_name,
                    "path": os.path.relpath(path, out),
                    "sha256": proj.sha256(content),
                    "size": len(content),
                })
                total += 1
            files.append({"index": index, "name": name, "assets": records})
        desc["files"] = files

    prj.meta = {
        "version": proj.PROJECT_VERSION,
        "tool": "ffta2tool",
        "rom": {
            "source": os.path.basename(args.rom),
            "sha256": _file_sha256(args.rom),
            "secure_area": _secure_area(args.rom),
        },
        "containers": descriptions,
    }
    prj.save()

    print("wrote %d assets to %s" % (total, prj.assets_dir))
    print("done")
    return 0


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _secure_area(path):
    """The "secure area disable" word of the header of a ROM.

    dsd does not keep that word, so it is read here and written back
    when the ROM is built again.
    """
    with open(path, "rb") as f:
        head = f.read(0x200)
    if len(head) < 0x200:
        return None
    return head[proj.SECURE_AREA_OFFSET:proj.SECURE_AREA_OFFSET + 2].hex()


# ---------------------------------------------------------------- status ---
def collect_changes(prj):
    """Return {container id: {file index: {asset name: bytes}}}."""
    out = {}
    for entry in prj.meta.get("containers", []):
        changed = {}
        for f in entry.get("files", []):
            assets = {}
            for asset in f.get("assets", []):
                path = os.path.join(prj.path, asset["path"])
                if not os.path.isfile(path):
                    continue
                with open(path, "rb") as fh:
                    content = fh.read()
                if proj.sha256(content) != asset["sha256"]:
                    assets[asset["name"]] = content
            if assets:
                changed[f["index"]] = assets
        if changed:
            out[entry["id"]] = changed
    return out


def cmd_status(args):
    prj = proj.Project(args.project)
    prj.load()
    changes = collect_changes(prj)
    if not changes:
        print("no changes")
        return 0
    count = 0
    for cid, changed in sorted(changes.items()):
        for index, assets in sorted(changed.items()):
            for name in sorted(assets):
                print("%s [%d] %s" % (cid, index, name))
                count += 1
    print("%d changed asset(s)" % count)
    return 0


# ----------------------------------------------------------------- build ---
def cmd_build(args):
    prj = proj.Project(args.project)
    prj.load()
    archive = prj.archive()
    changes = collect_changes(prj)
    if not changes:
        print("no changes, nothing to do")
        if not args.rom:
            return 0

    overrides = {}
    by_id = {}
    for container in prj.containers():
        by_id[container.id] = container
    for cid, changed in changes.items():
        container = by_id[cid]
        container.load(archive)
        new_members = container.build(changed)
        for role, data in new_members.items():
            key = container.members[role]
            slot, member = (int(x) for x in key.split(":"))
            overrides[(slot, member)] = data
        print("rebuilt %s" % cid)

    writer = ArchiveWriter(archive.idx, archive.bin)
    idx, bin_ = writer.build(overrides)
    with open(prj.master_idx, "wb") as f:
        f.write(idx)
    with open(prj.master_bin, "wb") as f:
        f.write(bin_)
    print("rebuilt the master archive (%d bytes)" % len(bin_))

    if not args.rom:
        return 0
    dsd = proj.find_dsd(args.dsd)
    if not dsd:
        print("error: the dsd executable was not found (use --dsd)",
              file=sys.stderr)
        return 1
    rom = os.path.abspath(args.rom)
    print("building %s ..." % rom)
    secure = prj.meta.get("rom", {}).get("secure_area")
    if secure:
        secure = bytes.fromhex(secure)
    proj.dsd_build(dsd, os.path.join(prj.rom_dir, "config.yaml"), rom,
                   secure_area=secure)
    print("done")
    return 0


# ------------------------------------------------------------------ main ---
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="extract and rebuild the assets of FFTA2")
    parser.add_argument("--dsd", help="path of the dsd executable")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("extract", help="create an editable project")
    p.add_argument("--rom", required=True, help="the ROM to read")
    p.add_argument("--out", required=True, help="the project directory")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing project")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("status", help="list the changed assets")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("build", help="rebuild the ROM")
    p.add_argument("--project", required=True)
    p.add_argument("--rom", help="the ROM to write (optional)")
    p.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
