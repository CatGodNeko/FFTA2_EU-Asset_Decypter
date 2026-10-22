"""The editable FFTA2 project.

A project is a directory that holds three things::

    project.json    the description of every container and asset
    rom/            the dsd project (the game data, untouched)
    assets/         the decoded, editable files (PNG, WAV)

``extract`` decodes the containers of the master archive into ``assets/``;
``build`` decodes the assets that were changed back into the native format,
puts the result into ``rom/`` and assembles a new ROM.

The native data always stays the source of truth: a container is rebuilt
from the *original* bytes plus the assets the user edited, so an asset
that is not touched keeps its original content byte for byte.
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess

from . import codecs
from .archive import Archive, ArchiveWriter
from .membernames import NAMES
from .pak import IdxPak, IdxPakWriter

PROJECT_VERSION = 1

#: container id -> short prefix used for the file names inside it
PREFIXES = {
    "menu/atl_rom/atl.pak": "atl",
    "menu/face_rom/face.pak": "face",
    "map/rom/TexData.dat": "map",
    "map/rom/PlttData.dat": "pltt",
}

#: (container id, index member, blob member, codec)
IDXPAK_CONTAINERS = [
    ("menu/atl_rom/atl.pak", "menu/atl_rom/atl_rom.idx",
     "menu/atl_rom/atl.pak", "top_sprite"),
    ("menu/face_rom/face.pak", "menu/face_rom/face_rom.idx",
     "menu/face_rom/face.pak", "unit_face"),
    ("map/rom/TexData.dat", "map/rom/TexData.idx",
     "map/rom/TexData.dat", "map_texture"),
    ("map/rom/PlttData.dat", "map/rom/PlttData.idx",
     "map/rom/PlttData.dat", "map_palette"),
]

#: members that are a container of their own
SINGLE_MEMBER_SUFFIX = ".efx"

#: the members that hold the graphics of the units
CHAR_MEMBERS = {
    "sst_idx": "char/rom/rom_idx/UnitSst.rom_idx",
    "sst_pak": "char/rom/pak/UnitSst.pak",
    "cg_idx": "char/rom/rom_idx/UnitCg.rom_idx",
    "cg_pak": "char/rom/pak/UnitCg.pak",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _member_index():
    """path -> "slot:member"."""
    out = {}
    for key, name in NAMES.items():
        out.setdefault(name, key)
    return out


MEMBERS = _member_index()


class Ctx(object):
    """What a codec is told about the file it is working on."""

    def __init__(self, name, index, extra=None):
        self.name = name
        self.index = index
        self.extra = extra if extra is not None else {}


# ------------------------------------------------------------------ base ---
class Container(object):
    """A group of archive members that decodes into editable assets."""

    kind = "container"

    def __init__(self, cid, codec_name, members):
        self.id = cid
        self.codec_name = codec_name
        self.codec = codecs.CODECS[codec_name]
        self.members = members      # role -> "slot:member"
        self.data = {}              # role -> bytes
        self.extra_context = {}     # file index -> extra codec context

    # -- the data --------------------------------------------------------
    def member_keys(self):
        return list(self.members.values())

    def load(self, archive):
        for role, key in self.members.items():
            self.data[role] = archive.member_bytes(key)

    def files(self):
        """Return the list of (index, name, data) of the container."""
        raise NotImplementedError

    def file_dir(self, index, name):
        """The directory (relative to assets/) that holds the assets."""
        raise NotImplementedError

    def build(self, changed):
        """Rebuild the container.

        *changed* maps a file index to {asset name: new bytes}.  Returns a
        dict role -> the new bytes of that member (empty if nothing
        changed).
        """
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    def make_ctx(self, index, name):
        return Ctx(name, index, self.context(index, name))

    def context(self, index, name):
        """Extra information the codec needs (e.g. a palette)."""
        return self.extra_context.get(index, {})


# --------------------------------------------------------------- idx/pak ---
class IdxPakContainer(Container):
    """A ".idx" index plus a ".pak" blob."""

    kind = "idxpak"

    def __init__(self, cid, codec_name, members, prefix=None):
        Container.__init__(self, cid, codec_name, members)
        self.prefix = prefix or PREFIXES.get(cid) or _short_name(cid)

    def files(self):
        ip = IdxPak(self.data["idx"], self.data["pak"])
        out = []
        for i, data in enumerate(ip.files):
            if data is None:
                continue
            out.append((i, "%s%03d" % (self.prefix, i), data))
        return out

    def file_dir(self, index, name):
        return "%s/%s" % (self.id, name)

    def build(self, changed):
        files = []
        for index, name, data in self.files():
            assets = changed.get(index)
            if not assets:
                continue
            new = self.codec.rebuild(self.make_ctx(index, name), data, assets)
            files.append((index, new))
        if not files:
            return {}
        ip = IdxPak(self.data["idx"], self.data["pak"])
        new_files = list(ip.files)
        for index, data in files:
            new_files[index] = data
        idx, pak = IdxPakWriter(self.data["idx"], new_files).build()
        return {"idx": idx, "pak": pak}


# --------------------------------------------------------------- member ---
class MemberContainer(Container):
    """A single archive member that decodes into assets."""

    kind = "member"

    def __init__(self, cid, codec_name, members, prefix=None):
        Container.__init__(self, cid, codec_name, members)
        self.prefix = prefix or _short_name(cid)

    def files(self):
        return [(0, self.prefix, self.data["data"])]

    def file_dir(self, index, name):
        return self.id

    def build(self, changed):
        assets = changed.get(0)
        if not assets:
            return {}
        new = self.codec.rebuild(self.make_ctx(0, self.prefix),
                                  self.data["data"], assets)
        return {"data": new}


def _short_name(path):
    """The last path element without its extension."""
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot > 0:
        base = base[:dot]
    return base


# ------------------------------------------------------------------- char ---
class CharContainer(Container):
    """The graphics of the units: a sprite table and a character graphic.

    The two ".idx" + ".pak" pairs hold the same number of files; the
    file *i* of the sprite table belongs to the file *i* of the character
    graphic (the sprite table points into it).  One file is one *sprite
    set*: the pictures of the units that share their artwork.
    """

    kind = "char"

    def __init__(self, cid, codec_name, members):
        Container.__init__(self, cid, codec_name, members)
        self.prefix = "unit"

    def _pairs(self):
        sst = IdxPak(self.data["sst_idx"], self.data["sst_pak"])
        cg = IdxPak(self.data["cg_idx"], self.data["cg_pak"])
        return sst, cg

    def files(self):
        sst, cg = self._pairs()
        out = []
        for i, data in enumerate(sst.files):
            if data is None or i >= len(cg.files) or cg.files[i] is None:
                continue
            out.append((i, "%s%03d" % (self.prefix, i), (data, cg.files[i])))
        return out

    def file_dir(self, index, name):
        return "%s/%s" % (self.id, name)

    def build(self, changed):
        sst, cg = self._pairs()
        new_sst = list(sst.files)
        new_cg = list(cg.files)
        touched = False
        for index, name, data in self.files():
            assets = changed.get(index)
            if not assets:
                continue
            sst_bytes, cg_bytes = self.codec.rebuild(
                self.make_ctx(index, name), data, assets)
            new_sst[index] = sst_bytes
            new_cg[index] = cg_bytes
            touched = True
        if not touched:
            return {}
        sst_idx, sst_pak = IdxPakWriter(self.data["sst_idx"], new_sst).build()
        cg_idx, cg_pak = IdxPakWriter(self.data["cg_idx"], new_cg).build()
        return {"sst_idx": sst_idx, "sst_pak": sst_pak,
                "cg_idx": cg_idx, "cg_pak": cg_pak}


# ------------------------------------------------------------- registry ----
def container_specs(archive=None):
    """The list of (container, description) of the game.

    *archive* is the master archive; it is used to find the members that
    hold a sound archive (their name says nothing about it).
    """
    out = []
    for cid, idx_path, pak_path, codec in IDXPAK_CONTAINERS:
        if idx_path not in MEMBERS or pak_path not in MEMBERS:
            continue
        members = {"idx": MEMBERS[idx_path], "pak": MEMBERS[pak_path]}
        out.append((IdxPakContainer(cid, codec, members),
                    {"id": cid, "kind": "idxpak", "codec": codec,
                     "members": members}))
    if all(path in MEMBERS for path in CHAR_MEMBERS.values()):
        members = dict((role, MEMBERS[path])
                       for role, path in CHAR_MEMBERS.items())
        out.append((CharContainer("char/rom", "char_sprite", members),
                    {"id": "char/rom", "kind": "char", "codec": "char_sprite",
                     "members": members}))
    for path, key in sorted(MEMBERS.items()):
        if not path.endswith(SINGLE_MEMBER_SUFFIX):
            continue
        members = {"data": key}
        out.append((MemberContainer(path, "efx_texture", members),
                    {"id": path, "kind": "member", "codec": "efx_texture",
                     "members": members}))
    if archive is not None:
        for path, key in sorted(MEMBERS.items()):
            try:
                data = archive.member_bytes(key)
            except KeyError:
                continue
            if data[:4] != b"SDAT":
                continue
            members = {"data": key}
            out.append((MemberContainer(path, "sdat", members),
                        {"id": path, "kind": "member", "codec": "sdat",
                         "members": members}))
    return out


# ---------------------------------------------------------------- project --
class Project(object):
    """An editable project on disk."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.meta = {}

    # -- paths -----------------------------------------------------------
    @property
    def rom_dir(self):
        return os.path.join(self.path, "rom")

    @property
    def assets_dir(self):
        return os.path.join(self.path, "assets")

    @property
    def meta_path(self):
        return os.path.join(self.path, "project.json")

    # -- the master archive ---------------------------------------------
    @property
    def master_idx(self):
        return os.path.join(self.rom_dir, "files", "master", "pc.idx")

    @property
    def master_bin(self):
        return os.path.join(self.rom_dir, "files", "master", "pc.bin")

    def archive(self):
        return Archive(self.master_idx, self.master_bin)

    # -- metadata --------------------------------------------------------
    def load(self):
        with open(self.meta_path, "r") as f:
            self.meta = json.load(f)
        return self.meta

    def save(self):
        with open(self.meta_path, "w") as f:
            json.dump(self.meta, f, indent=1, sort_keys=True)
            f.write("\n")

    # -- containers ------------------------------------------------------
    def containers(self):
        """Return the container objects of the project (data not loaded)."""
        out = []
        for entry in self.meta.get("containers", []):
            members = entry["members"]
            if entry["kind"] == "idxpak":
                out.append(IdxPakContainer(entry["id"], entry["codec"],
                                           members))
            elif entry["kind"] == "member":
                out.append(MemberContainer(entry["id"], entry["codec"],
                                           members))
            elif entry["kind"] == "char":
                out.append(CharContainer(entry["id"], entry["codec"],
                                         members))
            else:
                raise ValueError("unknown container kind %r" % entry["kind"])
        return out

    def asset_records(self):
        """Yield (container id, file index, asset record) of the project."""
        for entry in self.meta.get("containers", []):
            for f in entry.get("files", []):
                for asset in f.get("assets", []):
                    yield entry["id"], f["index"], asset


# ------------------------------------------------------------------ dsd ----
def find_dsd(explicit=None):
    """Locate the dsd executable."""
    if explicit:
        return explicit
    if os.path.isfile("/data/dsd") and os.access("/data/dsd", os.X_OK):
        return "/data/dsd"
    return shutil.which("dsd")


def dsd_extract(dsd, rom, out_dir):
    """Create the dsd project of a ROM."""
    os.makedirs(out_dir, exist_ok=True)
    subprocess.check_call([dsd, "rom", "extract", "--rom", rom,
                           "--output-path", out_dir])


def dsd_build(dsd, config, rom, secure_area=None):
    """Assemble a ROM out of a dsd project.

    *secure_area* is the "secure area disable" word of the header (the
    two bytes at 0x6C).  dsd does not keep that word, so it is written
    again after the build and the checksum of the header (the two bytes
    at 0x15E) is calculated anew.
    """
    subprocess.check_call([dsd, "rom", "build", "--config", config,
                           "--rom", rom])
    if secure_area:
        patch_header(rom, secure_area)


#: the offset of the "secure area disable" word of the ROM header
SECURE_AREA_OFFSET = 0x6C

#: the offset of the checksum of the header (bytes 0x00-0x15D)
HEADER_CRC_OFFSET = 0x15E


def crc16(data, init=0xFFFF):
    """The CRC-16 the Nintendo DS uses (polynomial 0xA001)."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def patch_header(rom_path, secure_area):
    """Write the header fields that dsd does not keep into a ROM.

    The "secure area disable" word of the header (0x6C) is always
    0xAE20 in a game card; dsd does not know it and writes 0 instead.
    The checksum of the header (0x15E, the CRC-16 of the bytes
    0x00-0x15D) is calculated again afterwards.
    """
    with open(rom_path, "r+b") as f:
        header = bytearray(f.read(0x200))
        if len(header) < 0x200:
            return False
        header[SECURE_AREA_OFFSET:SECURE_AREA_OFFSET + 2] = secure_area
        crc = crc16(header[:HEADER_CRC_OFFSET])
        header[HEADER_CRC_OFFSET:HEADER_CRC_OFFSET + 2] = \
            struct.pack("<H", crc)
        f.seek(0)
        f.write(bytes(header))
    return True
