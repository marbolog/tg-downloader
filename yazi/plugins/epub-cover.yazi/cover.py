"""Print the path of a cached cover image for an EPUB, or exit 1 if it has none.

Usage: python3 -I cover.py BOOK.epub
Stdlib only. The EPUB is untrusted: members are read as bytes (never extracted by
name) and sizes are capped. Cover lookup order:
  1. EPUB3 manifest item with properties="cover-image"
  2. EPUB2 <meta name="cover" content="ITEM_ID"> -> manifest item
  3. first image manifest item whose id or href contains "cover"
"""
import hashlib
import os
import posixpath
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tg-downloader-yazi"
IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}

CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
OPF_NS = "{http://www.idpf.org/2007/opf}"


def read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"{name} is {info.file_size} bytes, over the {limit} limit")
    return zf.read(info)


def parse_xml(raw: bytes) -> ET.Element:
    # Container/OPF files never need a DTD; refusing it rules out entity-expansion attacks.
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not allowed in EPUB metadata")
    return ET.fromstring(raw)


def find_cover_member(zf: zipfile.ZipFile) -> tuple[str, str] | None:
    container = parse_xml(read_member(zf, "META-INF/container.xml", MAX_XML_BYTES))
    rootfile = container.find(f".//{CONTAINER_NS}rootfile")
    if rootfile is None:
        return None
    opf_path = rootfile.get("full-path")
    opf = parse_xml(read_member(zf, opf_path, MAX_XML_BYTES))
    opf_dir = posixpath.dirname(opf_path)

    images = {}  # item id -> (member path, media type, properties)
    for item in opf.iterfind(f".//{OPF_NS}manifest/{OPF_NS}item"):
        media_type = item.get("media-type", "")
        if media_type in IMAGE_EXTENSIONS:
            member = posixpath.normpath(posixpath.join(opf_dir, item.get("href", "")))
            images[item.get("id", "")] = (member, media_type, item.get("properties", ""))

    for member, media_type, properties in images.values():
        if "cover-image" in properties.split():
            return member, media_type

    for meta in opf.iterfind(f".//{OPF_NS}metadata/{OPF_NS}meta"):
        if meta.get("name") == "cover" and meta.get("content") in images:
            member, media_type, _ = images[meta.get("content")]
            return member, media_type

    for item_id, (member, media_type, _) in images.items():
        if "cover" in item_id.lower() or "cover" in member.lower():
            return member, media_type
    return None


def main(epub_path: str) -> int:
    stat = os.stat(epub_path)
    key = hashlib.sha1(f"{os.path.realpath(epub_path)}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    for cached in CACHE_DIR.glob(f"{key}.*"):
        print(cached)
        return 0

    with zipfile.ZipFile(epub_path) as zf:
        found = find_cover_member(zf)
        if found is None:
            print("no cover declared in EPUB manifest", file=sys.stderr)
            return 1
        member, media_type = found
        data = read_member(zf, member, MAX_IMAGE_BYTES)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{key}{IMAGE_EXTENSIONS[media_type]}"
    out.write_bytes(data)
    print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # any malformed EPUB: report to the preview pane, don't crash
        print(f"cannot read EPUB cover: {exc}", file=sys.stderr)
        sys.exit(2)
