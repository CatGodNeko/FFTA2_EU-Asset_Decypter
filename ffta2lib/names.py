"""Recover the virtual path of every member of the FFTA2 master archive.

The archive stores no names; the game hashes the path at runtime with a
10 bit CRC.  We therefore rebuild the name space from the ROM's own code:

  * every literal string in arm9/arm7/overlays that looks like a file path
  * every printf style template found there, expanded over the numeric
    ranges the game actually uses

A candidate is accepted when its hash lands on a live slot *and* the slot's
stored character/position signature agrees with the candidate.  For slots
that hold several files the signature is what makes the assignment unique.
"""

import itertools
import json
import os
import re
import string

from .archive import Archive, filename_crc

BASE = "/data/FFTA2"

STR_RE = re.compile(rb"[\x20-\x7e]{3,}")
CONV_RE = re.compile(r"%[-+0-9.]*[diuxXscf]")

EXTENSIONS = (
    "bin", "pak", "idx", "rom_idx", "tex", "plt", "pal", "ebn", "obn", "sbn",
    "sdat", "efx", "dat", "bhd", "ntfp", "ntft", "ndsp", "nd", "sst", "cg",
    "cmp", "tbl", "cso", "pkt", "tpl", "raw", "bmd", "obj", "map", "wav",
)
EXT_RE = re.compile(r"\.(" + "|".join(EXTENSIONS) + r")$", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9_./+\-]{2,90}$")

# templates that are built from two separate strings in the ROM
EXTRA_TEMPLATES = [
    "effect/event/e%03u.efx",
    "effect/map_common/a%03u.efx",
    "effect/magic/m%03u.efx",
    "effect/titleloop/t%03u.efx",
    "sound/common/menu_common.sdat",
    "sound/common/battle_common.sdat",
    "sound/common/menu_ext.sdat",
    "sound/common/menu_hensei.sdat",
]

# formats the game uses for file indices
RANGES = {
    "%d": range(0, 64),
    "%2d": range(0, 100),
    "%02d": range(0, 100),
    "%3d": range(0, 1000),
    "%03d": range(0, 1000),
    "%4d": range(0, 10000),
    "%04d": range(0, 10000),
    "%4.4d": range(0, 10000),
    "%3.3x": range(0, 4096),
    "%2.2d": range(0, 100),
    "%1d": range(0, 10),
    "%03u": range(0, 1000),
    "%u": range(0, 1000),
}


def _blobs(base=BASE):
    out = {}
    paths = ["arm9/arm9.bin", "arm7/arm7.bin"]
    paths += [f"arm9_overlays/ov{i:03d}.bin" for i in range(32)]
    for p in paths:
        fp = os.path.join(base, p)
        if os.path.exists(fp):
            out[p] = open(fp, "rb").read()
    return out


def harvest(base=BASE):
    """All printable strings in the ROM's code sections."""
    out = set()
    for data in _blobs(base).values():
        for m in STR_RE.finditer(data):
            s = m.group().decode("ascii")
            if len(s) >= 3:
                out.add(s)
    return out


def plausible(name):
    if not name or "%" in name:
        return False
    if not NAME_RE.match(name):
        return False
    if not EXT_RE.search(name):
        return False
    return True


def expand(tmpl):
    """Expand a numeric-only printf template."""
    keys = CONV_RE.findall(tmpl)
    if not keys:
        yield tmpl
        return
    lists = []
    for k in keys:
        if k in RANGES:
            lists.append([k % v for v in RANGES[k]])
        else:
            lists.append([])  # %s / unknown -> not expandable
    if any(not l for l in lists):
        return
    for combo in itertools.product(*lists):
        s = tmpl
        for k, v in zip(keys, combo):
            s = s.replace(k, v, 1)
        yield s


def build_candidates(base=BASE):
    """Return (exact_names, numeric_names)."""
    raw = harvest(base)
    exact, numeric = set(), set()
    for s in raw:
        if CONV_RE.search(s):
            if '/' in s and plausible(re.sub(CONV_RE, "0", s)):
                numeric.update(expand(s))
        elif plausible(s):
            exact.add(s)
    for t in EXTRA_TEMPLATES:
        if plausible(re.sub(CONV_RE, "0", t)):
            numeric.update(expand(t))
    return exact, numeric


class NameResolver:
    def __init__(self, arc: Archive, base=BASE):
        self.arc = arc
        exact, numeric = build_candidates(base)
        self.exact, self.numeric = exact, numeric
        self.assignments = {}      # (slot, member) -> name
        self.ambiguous = set()
        self._resolve()

    # ------------------------------------------------------------------
    def _match_member(self, name, crc):
        """Return the member index of `crc` that `name` claims, or None."""
        slot = self.arc.slots[crc]
        if slot is None:
            return None
        want = name.encode()
        fallback = None
        for j, (data, chr_, chr_pos) in enumerate(self.arc.get(crc)):
            if chr_pos < 0:
                if fallback is None:
                    fallback = j
                continue
            if chr_pos < len(want) and want[chr_pos] == chr_:
                return j
        return fallback

    def _resolve(self):
        # exact names win over numeric expansions
        for tier, names in (("exact", self.exact), ("numeric", self.numeric)):
            collisions = {}
            for name in sorted(names):
                crc = filename_crc(name)
                j = self._match_member(name, crc)
                if j is None:
                    continue
                key = (crc, j)
                if key in self.assignments:
                    continue
                self.assignments[key] = name
                collisions.setdefault(crc, []).append(name)
            if tier == "exact":
                # remember exact names that collided on the same slot
                self.exact_collisions = {k: v for k, v in collisions.items() if len(v) > 1}

    # ------------------------------------------------------------------
    def name_for(self, slot, member):
        return self.assignments.get((slot, member))

    def coverage(self):
        total = 0
        for i in range(self.arc.num_slots):
            if self.arc.slots[i] is None:
                continue
            total += len(self.arc.get(i))
        return total, len(self.assignments)


def main():
    arc = Archive(os.path.join(BASE, "files/master/pc.idx"),
                  os.path.join(BASE, "files/master/pc.bin"))
    res = NameResolver(arc)
    total, named = res.coverage()
    print(f"named {named} / {total} archive members")
    return res


if __name__ == "__main__":
    main()
