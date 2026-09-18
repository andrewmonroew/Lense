#!/usr/bin/env python3
"""Bring every catalog thumbnail down to policy (src/core/image_assets.py).

A one-off for the originals scraped at full resolution, and safe to re-run: an
image already within policy re-encodes to the same thing. When an opaque PNG
becomes a JPEG the catalog JSON is rewritten to match, so no entry is left
pointing at a file that no longer exists.

    python tools/normalize_catalog_images.py --dry-run
    python tools/normalize_catalog_images.py
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QGuiApplication, QImage   # noqa: E402
from src.core import image_assets                  # noqa: E402

# Qt's image codecs are plugins, and loading them needs an application object --
# without one this segfaults the moment it tries to encode.
_app = QGuiApplication.instance() or QGuiApplication([])

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "src", "data")
IMAGES = os.path.join(DATA, "images")


def catalog_files():
    return sorted(glob.glob(os.path.join(DATA, "*.json")))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"policy: longest edge {image_assets.MAX_EDGE}px, "
          f"under {image_assets.MAX_BYTES // 1024}KB, "
          f"PNG only where there is transparency\n")

    renames = {}          # old catalog-relative path -> new one
    before = after = 0
    failures = []

    for path in sorted(glob.glob(os.path.join(IMAGES, "*"))):
        name = os.path.basename(path)
        stem, _ = os.path.splitext(name)
        original_size = os.path.getsize(path)
        before += original_size

        encoded = image_assets.encode_thumbnail(QImage(path))
        if encoded is None:
            failures.append(name)
            after += original_size
            continue
        data, extension = encoded
        after += len(data)

        change = f"{original_size/1024:8.0f}KB -> {len(data)/1024:6.0f}KB"
        if extension != os.path.splitext(name)[1].lower():
            change += f"   {os.path.splitext(name)[1]} -> {extension}"
            renames[f"images/{name}"] = f"images/{stem}{extension}"
        print(f"  {name:44} {change}")

        if not args.dry_run:
            os.remove(path)
            with open(os.path.join(IMAGES, stem + extension), "wb") as handle:
                handle.write(data)

    print(f"\n{before/1048576:.1f} MB -> {after/1048576:.1f} MB "
          f"({100 - after * 100 / before:.0f}% smaller)")
    if failures:
        print(f"unreadable, left alone: {', '.join(failures)}")

    if renames:
        print(f"\n{len(renames)} catalog entries need their imagePath updated"
              f"{' (dry run: not written)' if args.dry_run else ''}")
        if not args.dry_run:
            for path in catalog_files():
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                entries = loaded.values() if isinstance(loaded, dict) else loaded
                touched = False
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("imagePath") in renames:
                        entry["imagePath"] = renames[entry["imagePath"]]
                        touched = True
                if touched:
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(loaded, handle, indent=2)
                        handle.write("\n")
                    print(f"  updated {os.path.basename(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
