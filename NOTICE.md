# Third-party notices

## PySide6 / Qt

Lense is built on [PySide6](https://doc.qt.io/qtforpython/), the official Python
binding for Qt, used under the **LGPL v3**. Qt itself is not modified. The LGPL
requires that anyone receiving a binary of this application be able to replace
the Qt libraries inside it — building from this source with a different PySide6
version, per the instructions in the README, satisfies that.

## Equipment catalog data

`src/data/*.json` holds published specifications for networking and camera
hardware — port counts, PoE classes, wattages, dimensions — compiled from each
manufacturer's public documentation (techspecs.ui.com, reolink.com and the
product pages linked in each entry's `productUrl`).

Specifications are facts and are not claimed as anyone's creative work. The
compilation, structure and the PoE model built on top of it are ours.

## Product photography

The catalog includes a thumbnail per entry (`imagePath`) in `src/data/images/`.
Those photographs are the property of **Ubiquiti Inc.** and **Reolink**
respectively, taken from their published product pages. They are included here
solely to identify the equipment a user is selecting, and are reduced to
low-resolution thumbnails — 512 px on the longest side, under 150 KB — which is
no more than the interface displays.

No claim of ownership is made over them. If a rights holder would prefer their
images not appear here, contact the repository owner and they will be removed;
`src/core/image_assets.py` and `tools/normalize_catalog_images.py` handle the
catalog's imagery in one place, and the application runs correctly with no
thumbnails at all.

## Trademarks

Ubiquiti, UniFi, Reolink and all product names are trademarks of their
respective owners. They are used here only to identify the equipment this tool
helps design with. Lense is not affiliated with, endorsed by, or sponsored by
any of them.

## Splash chime

`src/data/Weird2.wav` was written for this project by its author and is
included with permission.
