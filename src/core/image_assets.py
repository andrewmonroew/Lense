"""One size and format policy for every catalog thumbnail.

Vendor product shots arrive as 3000x3000 PNGs of 3 MB each. Lense never draws
one bigger than 220 px wide (properties sidebar) or 96 px (catalog manager), so
carrying the full-resolution originals cost 80 MB in the repository and the same
again inside every executable built from it -- for pixels nothing ever displays.

So everything landing in the catalog is normalised on the way in, whether it came
from the vendor scrape or from a user picking a file in the Catalog Manager:

    - scaled down to fit MAX_EDGE (never scaled UP -- a small image stays small)
    - transparency preserved as PNG; anything opaque re-encoded as JPEG, which is
      far smaller for photographs
    - squeezed under MAX_BYTES, by dropping JPEG quality or, for PNGs where
      quality is not a dial, by stepping the edge down

MAX_EDGE is deliberately about double the largest on-screen use, so thumbnails
stay crisp on a HiDPI display without storing a poster.
"""

import os

from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QImage

MAX_EDGE = 512            # px, longest side
MAX_BYTES = 150 * 1024    # 150 KB per thumbnail

JPEG_QUALITIES = (85, 75, 65, 55)      # tried in order for opaque images
PNG_FALLBACK_EDGES = (512, 448, 384, 320, 256)   # transparency has no quality dial

# What a user is allowed to pick in the Catalog Manager. Qt reads more than this,
# but these are the ones worth offering.
ACCEPTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _encode(image, fmt, quality=-1):
    """Encode in memory so candidate sizes can be compared without touching disk.

    `storage` has to outlive the QBuffer: QBuffer keeps a pointer to the byte
    array it was handed, so passing a temporary QByteArray straight into the
    constructor hands it a pointer to something Python frees immediately, and the
    first write segfaults the interpreter.
    """
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QBuffer.WriteOnly)
    saved = image.save(buffer, fmt, quality)
    buffer.close()
    return bytes(storage) if saved else None


def _fit(image, edge):
    if max(image.width(), image.height()) <= edge:
        return image
    return image.scaled(edge, edge, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def encode_thumbnail(image):
    """(bytes, extension) for an image reduced to policy, or None if unreadable.

    Transparency decides the format, not the source file's extension: a PNG of an
    opaque photograph becomes a JPEG a tenth the size, while a product shot cut
    out against transparency stays a PNG so it doesn't gain a white box.
    """
    if image is None or image.isNull():
        return None

    if image.hasAlphaChannel():
        for edge in PNG_FALLBACK_EDGES:
            if edge > MAX_EDGE:
                continue
            data = _encode(_fit(image, edge), "PNG")
            if data is not None and len(data) <= MAX_BYTES:
                return (data, ".png")
        # Nothing got under the cap -- keep the smallest attempt rather than fail,
        # since an oversized thumbnail still beats no thumbnail.
        data = _encode(_fit(image, PNG_FALLBACK_EDGES[-1]), "PNG")
        return (data, ".png") if data else None

    fitted = _fit(image, MAX_EDGE)
    smallest = None
    for quality in JPEG_QUALITIES:
        data = _encode(fitted, "JPG", quality)
        if data is None:
            continue
        if len(data) <= MAX_BYTES:
            return (data, ".jpg")
        smallest = data
    return (smallest, ".jpg") if smallest else None


def write_thumbnail(source_path, images_dir, stem):
    """Normalise `source_path` into `images_dir/<stem>.<ext>`.

    Returns the catalog-relative path ("images/foo.jpg"), or None if the file
    could not be read as an image. Any previous thumbnail for this stem in a
    different format is removed, so swapping a PNG for a JPEG can't leave the old
    one behind to be picked up later.
    """
    encoded = encode_thumbnail(QImage(source_path))
    if encoded is None:
        return None
    data, extension = encoded

    os.makedirs(images_dir, exist_ok=True)
    for suffix in set(ACCEPTED_SUFFIXES) | {".png", ".jpg"}:
        stale = os.path.join(images_dir, stem + suffix)
        if suffix != extension and os.path.exists(stale):
            os.remove(stale)

    destination = os.path.join(images_dir, stem + extension)
    with open(destination, "wb") as handle:
        handle.write(data)
    return f"images/{stem}{extension}"
