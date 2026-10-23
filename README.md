# ffta2tool — an editable project for FFTA2

`ffta2tool` unpacks a ROM of *Final Fantasy Tactics Advance 2* into files
you can open in an image editor or a sound editor, and packs your changes
back into a new ROM.

```
python3 tools/ffta2tool.py extract --rom FFTA2-EU_A6FPGD_00.nds --out project
python3 tools/ffta2tool.py status  --project project
python3 tools/ffta2tool.py build   --project project --rom patched.nds
```

Only the Python standard library is used (no PIL, no numpy); the PNG and
WAV readers and writers are in `tools/ffta2lib/`.  The ROM itself is
unpacked and packed with [`dsd`](https://github.com/AetiasHax/ds-decomp), which
has to be in the path (or be given with `--dsd`).

## The project directory

```
project/
├── project.json    every container, every file and every asset
├── rom/            the dsd project: the game data, untouched
└── assets/         the decoded, editable files (PNG, WAV)
```

`project.json` remembers the size and the checksum of every asset, so
`status` can tell which files were changed and `build` only has to decode
the ones that were.

### The assets

The `assets/` tree mirrors the paths of the game, so it is easy to find the
file you want to change:

```
assets/
├── char/rom/unit000/unit000_spr000.png   the sprites of a unit
│                      unit000_pal00.png   the palettes of the unit
├── menu/atl_rom/atl.pak/atl000/atl000_s0.png   the icons of the menu
│                             atl000_pal.png
├── menu/face_rom/face.pak/face001/face001_tex.png  the portraits
├── map/rom/TexData.dat/map000/map000_tex.png      the textures of the maps
├── map/rom/PlttData.dat/pltt000/pltt000.png       the palettes of the maps
├── effect/magic/m001.efx/m001_0.png               the textures of the effects
└── sound/event/event_se.pak/event_se_9_w0.wav      the sounds
```

## How a build works

The native data stays the source of truth.  A container is rebuilt from
the **original** bytes plus the assets that were changed; an asset that
was not touched keeps its content byte for byte.  A ROM that is built
without any change is therefore identical to the ROM it came from
(`md5 54a3e8694acf490357a1d32a407e03b1` for the European release).

Some of the formats of the game are compressed with an encoder that is not
byte stable (the LZSS encoder of the game picks a different match than
ours).  Such data is therefore never compressed again when it did not
change — the original bytes are kept.

The header of the ROM holds a word that `dsd` does not keep (the "secure
area disable" word at `0x6C`); it is read when the project is created and
written again when the ROM is built, together with a new checksum of the
header.

## A change from beginning to end

```
python3 tools/ffta2tool.py extract --rom FFTA2-EU_A6FPGD_00.nds --out project
#   edit a file in project/assets/ with any image or sound editor
python3 tools/ffta2tool.py status  --project project
python3 tools/ffta2tool.py build   --project project --rom patched.nds
```

`status` prints the assets whose checksum differs from the one that was
recorded when the project was created, e.g.

```
char/rom [0] unit000_spr000.png
1 changed asset(s)
```

`build` writes only those assets back into the game data and then assembles
the ROM.  Without `--rom` it only rebuilds the game data inside the project,
which is handy when you want to look at the result of the codec alone.

Rules for the files you edit:

* keep the **size** of a picture — the game stores the pieces of a sprite in
  a fixed layout;
* an **indexed** PNG keeps its indices, so you can paint with the palette of
  the picture; a picture that is stored as truecolour is quantised against
  the palette of the unit when it is written back;
* the loop points of a wave live in the `smpl` chunk of the WAV file.

## The formats

| Container | Codec | What it holds |
|-----------|-------|---------------|
| `char/rom` | `char_sprite` | the graphics of the units: the sprite table (`UnitSst`) and the character graphic (`UnitCg`) — 324 sprite sets, 7824 assets |
| `menu/atl_rom/atl.pak` | `top_sprite` | the icons of the menu — 261 files, 1566 assets |
| `menu/face_rom/face.pak` | `unit_face` | the portraits of the units — 283 files, 566 assets |
| `map/rom/TexData.dat` | `map_texture` | the textures of the battle maps — 89 files |
| `map/rom/PlttData.dat` | `map_palette` | the palettes of the battle maps — 138 files |
| `**/*.efx` | `efx_texture` | the textures of the effects — 273 containers, 1214 assets |
| `sound/**`, `battle/**`, `worldmap/**` | `sdat` | the sounds of the game — 95 containers, 1674 assets |

That is 373 containers, 1454 files and 13071 assets; every one of them
survives a round trip byte for byte.

### Pictures

A picture that is a pure palette is written as an **indexed** PNG: the
index of a pixel is the number the game stores, and the palette of the
picture holds the colour of that number.  A picture that was not changed
therefore survives a round trip exactly, even when the palette of the game
holds the same colour twice.

Pictures that have more than one palette (the graphics of the units) get
a separate `*_pal*.png` for every palette.

A picture that is stored as truecolour (the effect textures) is written as
a truecolour PNG with an alpha channel; a pixel that was not changed keeps
the byte it had.

### Sounds

The waves of the sound archives are written as WAV files.  The samples of
a wave are 4 bit IMA ADPCM; the encoder of the game is not byte stable, so
a wave whose samples did not change keeps the bytes it had.

The loop points of a wave are stored in the `smpl` chunk of the WAV file.

## The graphics of the units (`char/rom`)

The graphics of the units live in two files per *sprite set*:

* `UnitSst` — the *sprite table*: a binary tree that points at the palette
  of the set and at the map of its sprites.
* `UnitCg` — the *character graphic*: the compressed pixels of the
  sprites.

One sprite set is used by one or more units; the units that share the
artwork have a palette of their own.

The assets of a sprite set are

```
assets/char/rom/unit000/unit000_pal00.png    the palettes of the set
assets/char/rom/unit000/unit000_pal01.png    (32 colours each)
assets/char/rom/unit000/unit000_spr000.png   the sprites of the set
assets/char/rom/unit000/unit000_spr001.png
...
```

### The pixels of a sprite

A pixel of a sprite is one byte:

```
bit 7 .. 5   the shade: a brightness level (0 is the darkest)
bit 4 .. 0   the index of the colour in the 32 colour palette
```

The byte 0 is the empty pixel (the game shows it transparent), every other
byte is a colour: the colour of the palette, darkened by the shade.  The
shade works like a quarter step — the shade 7 is the colour of the
palette itself, the shade 5 is 75 % of it, the shade 3 is 50 % and the
shade 1 is 25 %.

A sprite is therefore written as an indexed PNG whose index is the byte
the game stores, with a palette of 256 entries that holds the colour of
every possible byte.  The picture is made with the *first* palette of the
set; the other palettes are written as pictures of their own.

**To change a sprite:** open the `*_spr*.png` and draw.  An indexed PNG
keeps its indices, so the picture can be changed without touching the
palette; a picture that is stored as truecolour is quantised against the
palette of the unit.

**To change the colours of a unit:** open the `*_pal*.png` of the palette
and change a colour.  The sprites of the set are drawn with the first
palette, so this is the way to recolour a whole unit.

### The pieces of a sprite

A sprite is not stored as one picture but as a list of *pieces* (a head, a
body, an arm, …).  The PNG holds the bounding box of all pieces; the
pieces are cut out of it again when the ROM is built.  The picture
therefore has to keep its size.

## The tests

```
python3 tools/selftest.py      # every file of the game survives a round trip
python3 tools/verify_edit.py    # a change really arrives in the new ROM
```

`selftest.py` decodes every file of the game and encodes it again; the
result has to be byte identical to the original.  `verify_edit.py` paints
a pixel into a picture of the menu and a pixel into a sprite of a unit,
builds a ROM and reads the pictures back out of it.

## The layout of the code

```
tools/ffta2tool.py       the command line tool
tools/ffta2lib/
├── archive.py           the master archive (pc.idx + pc.bin)
├── pak.py               the ".idx" + ".pak" containers
├── codecs.py            one codec per format of the game
├── project.py           the project: containers, assets, the dsd build
├── charsprite.py        the graphics of the units (sprite table, sprites)
├── image.py             the PNG reader and writer
├── lzss.py              the LZSS codec of the game
├── sdat.py              the sound archives of the game
├── wav.py               the WAV reader and writer
└── membernames.py       the names of the files of the game
```

A codec has two methods:

```python
extract(ctx, data)          -> {asset name: bytes}
rebuild(ctx, data, assets)  -> the new native bytes
```

`ctx` tells the codec the name and the index of the file it works on;
`assets` holds the new content of the assets the user changed.
