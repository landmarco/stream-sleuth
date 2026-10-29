"""Track metadata: id3/vorbis tags via mutagen, filename fallback.

Many library rips (including the test tracks) are untagged; the fallback
strips leading track numbers ("01 I'm A Monk" -> "I'm A Monk") and leaves
the rest of the stem as the title.
"""

import os
import re

import mutagen


def read_metadata(path):
    """(artist, title, album) from tags, else from the filename."""
    artist = title = album = ""
    try:
        f = mutagen.File(path, easy=True)
        if f is not None and f.tags:
            g = f.tags.get
            artist = _first(g("artist"))
            title = _first(g("title"))
            album = _first(g("album"))
    except Exception:
        pass
    if not artist and not title:
        title = title_from_filename(path)
        artist = ""
    return artist, title, album


def _first(v):
    """mutagen easy tags are lists; normalize to a single string."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v).strip() if v else ""


def title_from_filename(path):
    """Strip track-number prefixes, keep anything ambiguous like "10.000 hands"."""
    stem = os.path.splitext(os.path.basename(path))[0]
    s = stem.strip()
    if s.startswith("#"):
        s = s[1:].strip()
    m = re.match(r"^(\d{1,3}(?:[-.]\d{1,3})?)[.\s-]\s*(.*)$", s)
    if m and m.group(2) and not m.group(2)[0].isdigit():
        s = m.group(2).strip()
    return s


_LEADING = None  # legacy import guard; use title_from_filename directly
