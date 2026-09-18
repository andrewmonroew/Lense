# Lense

A design tool for low-voltage networks on a floor plan — cameras, access points,
switches, NVRs, racks, wall drops, and the cable between them.

It is not a drawing program. Every line you draw is a cable that knows what it
connects, how long it really is, whether the device on the end can power up, and
what it costs to terminate. It answers three questions a floor plan alone cannot:

- **How much cable do I really pull?** Runs are measured on the calibrated plan,
  then charged the vertical rise and service loop each endpoint actually costs —
  so a 150 ft run to a wall drop reports the 185 ft you will pull, and that is
  what counts against the 328 ft limit.
- **Will every PoE device turn on?** Power is resolved along the chain, per port
  and per class, including switches that are themselves powered over Ethernet and
  re-source a smaller budget downstream. The trap it was built to catch: a USW
  Flex on a PoE+ port can offer 20 W, and a U7 Pro needs 21.
- **What does the job cost?** A live takeoff of equipment, footage, and the
  termination hardware each connection needs — plugs, jacks, patch cables.

Status: **1.0 alpha.** Usable, under active development, save often.

## Getting it

**Windows:** download `Lense-<version>.exe` from
[Releases](../../releases). One self-contained file, nothing to install.
It is unsigned, so SmartScreen warns on first run — *More info → Run anyway*.

Read **READ ME FIRST.txt** (attached to every release) before you start. It is a
two-minute read and covers the entire workflow.

**From source:** see below.

## Running from source

Requires Python 3.10 or newer.

```bash
git clone <this repo>
cd Lense
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

That is everything — the equipment catalog, its thumbnails, the icons and the
splash chime are all in the repository.

Catalog thumbnails are held to one policy (`src/core/image_assets.py`): 512 px
on the longest side and under 150 KB, transparency kept as PNG and everything
else re-encoded as JPEG. That applies both to what ships here and to any image
a user picks in the Catalog Manager, so the catalog cannot quietly grow a set of
3 MB product shots to display at 220 px. `tools/normalize_catalog_images.py`
re-applies it in bulk.

## Building the executable

CI builds Windows on every push (see below), so you rarely need this.

```bash
build_windows.bat     # Windows -> dist\Lense.exe
./build.sh            # Linux/macOS -> dist/Lense
```

Both produce a single self-contained binary with the catalog, icons and splash
chime baked in, and copy `READ ME FIRST.txt` beside it.

An executable can only be built on the platform it targets — PyInstaller does not
cross-compile, which is what GitHub Actions is for.

## Continuous integration

[`.github/workflows/build.yml`](.github/workflows/build.yml):

| Trigger | What happens |
| --- | --- |
| push to `main` | Tests on Linux, then tests + builds `Lense.exe` on Windows, attached to the run as an artifact (90 days) |
| push a tag `v*` | The same, plus a GitHub Release with the exe and the quick-start sheet |
| pull request | Tests only |

Cutting a release:

```bash
git tag v1.0-alpha
git push origin v1.0-alpha
```

Only the release job is granted write access to the repository, and it runs only
for tags. Everything else is read-only, and nothing in the release path depends
on a third-party action.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python tests/test_regression.py
```

One file, 400+ assertions, no framework. It drives the real widgets and the real
mouse and key handlers rather than calling APIs underneath them — a feature you
cannot reach from the UI is a feature that does not work, and this suite exists
because that distinction has bitten this project before.

## Layout

```
main.py                 entry point
src/
  core/                 no Qt widgets: lengths, PoE resolution, termination,
                        undo, settings-backed defaults, zoom maths
  graphics/             QGraphicsItems -- how things draw and behave on canvas
  ui/                   windows, docks, dialogs, the canvas view
  data/                 equipment catalog (JSON), icons, splash chime
tests/                  the regression suite
tools/                  one-off maintenance scripts
Lense.spec              PyInstaller build definition
```

The split that matters: `core/` holds the models and the arithmetic and imports
no widgets, so the rules can be tested without a window. `graphics/` and `ui/`
are the parts you can see.

## Licensing

Source is published for viewing. It is **not** open source — see
[LICENSE](LICENSE). Third-party components, catalog data and trademarks are
covered in [NOTICE.md](NOTICE.md).

Lense is a design aid, not an engineering authority. Verify against
manufacturer specifications and applicable code before buying or installing.
