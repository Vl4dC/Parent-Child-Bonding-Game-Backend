"""
Study material uploaded by a learner: a digital book (PDF) or photos of its pages.

  read_files()   upload -> Material: one text per page.
                   - text PDFs are read locally with PDFium: free and fast
                   - pages that are pictures (scans, photos) are rendered and read by a vision model
  select_pages() what the learner asks to study -> the pages to build the lesson from, found locally:
                   "pages 40-55"      those pages
                   "chapter 3"        that chapter, using the headings found in the book
                   "photosynthesis"   the best-matching pages (BM25 word search) and their neighbours
                 A topic the book doesn't mention raises NotInMaterial instead of guessing.

Everything stays in memory (store.py); page images are never kept, only their text.
"""
from __future__ import annotations

import base64
import io
import logging
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

log = logging.getLogger("materials")

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024  # all files of one upload together (held in memory while reading)
MAX_FILES = 20
MAX_PDF_PAGES = 600
MAX_PIXELS = 60_000_000
MAX_VISION_PAGES = 100      # pages read by the vision model per upload (cost and time cap)
VISION_WORKERS = 8
MIN_TEXT_CHARS = 40         # a PDF page with less text than this is treated as a picture
IMAGE_MAX_SIDE = 1600
JPEG_QUALITY = 80

EXCERPT_CHARS = 30_000      # how much of the book goes into one lesson prompt
EXCERPT_PAGES = 12
SEARCH_SEEDS = 6
RANGE_PAGES = 30

VisionReader = Callable[[bytes], str]  # JPEG bytes -> the text on that page


class BadUpload(ValueError):
    """Something is wrong with what was uploaded. `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class NotInMaterial(ValueError):
    """The learner asked for something the upload doesn't contain."""


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Page(BaseModel):
    number: int                                  # 1-based, as shown in a PDF viewer
    text: str
    via: Literal["text", "vision", "none"]       # where the text came from; "none" = blank or unreadable


class Chapter(BaseModel):
    title: str
    start: int
    end: int


class Material(BaseModel):
    id: str = ""
    name: str
    kind: Literal["pdf", "photos"]
    pages: list[Page]
    chapters: list[Chapter] = []
    contents: list[int] = []      # contents/index pages: they name every topic without teaching it, so search skips them
    warnings: list[str] = []

    def info(self) -> MaterialInfo:
        count = Counter(p.via for p in self.pages)
        return MaterialInfo(id=self.id, name=self.name, kind=self.kind, page_count=len(self.pages),
                            text_pages=count["text"], vision_pages=count["vision"], blank_pages=count["none"],
                            chapters=self.chapters, warnings=self.warnings)


class MaterialInfo(BaseModel):
    id: str
    name: str
    kind: Literal["pdf", "photos"]
    page_count: int
    text_pages: int = Field(..., description="Pages whose text was read directly from the PDF (free)")
    vision_pages: int = Field(..., description="Pages that were pictures and were read by the vision model")
    blank_pages: int = Field(..., description="Pages with no readable text")
    chapters: list[Chapter] = Field(..., description="Chapter headings found in the book; ask for one by name, e.g. 'chapter 3'")
    warnings: list[str]


class Selection(BaseModel):
    pages: list[int]
    how: Literal["pages", "chapter", "search"]
    label: str
    text: str = Field(..., description="The pages' text, each page headed '[Page N]'")
    truncated: bool = Field(False, description="More pages matched than fit in one lesson")


# --------------------------------------------------------------------------
# Reading an upload
# --------------------------------------------------------------------------
def read_files(files: list[tuple[str, bytes]], vision: VisionReader) -> Material:
    if not files:
        raise BadUpload("Upload a PDF, or one or more photos of pages")
    if len(files) > MAX_FILES:
        raise BadUpload(f"Upload at most {MAX_FILES} files at once", 413)
    for name, data in files:
        if not data:
            raise BadUpload(f"{name} is empty")
        if len(data) > MAX_FILE_BYTES:
            raise BadUpload(f"{name} is larger than {MAX_FILE_BYTES // 2**20} MB", 413)
    if sum(len(d) for _, d in files) > MAX_TOTAL_BYTES:
        raise BadUpload(f"The files add up to more than {MAX_TOTAL_BYTES // 2**20} MB; upload fewer at a time", 413)
    pdfs = [f for f in files if b"%PDF-" in f[1][:1024]]
    if pdfs:
        if len(files) > 1:
            raise BadUpload("Upload one PDF at a time, or photos only")
        return read_pdf(pdfs[0][0], pdfs[0][1], vision)
    return read_photos(files, vision)


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_jpeg(img: Image.Image) -> bytes:
    """Upright, flat RGB, at most IMAGE_MAX_SIDE px: what the vision model reads."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    else:
        img = img.convert("RGB")
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def transcribe(jpegs: dict[int, bytes], vision: VisionReader) -> tuple[dict[int, str], list[int]]:
    """Read pages with the vision model, several at a time. Returns (page -> text, pages that failed)."""
    texts: dict[int, str] = {}
    failed: list[int] = []

    def one(item: tuple[int, bytes]) -> None:
        number, jpeg = item
        try:
            texts[number] = clean(vision(jpeg))
        except Exception:
            log.exception("vision read of page %d failed", number)
            failed.append(number)

    with ThreadPoolExecutor(VISION_WORKERS) as pool:
        list(pool.map(one, jpegs.items()))
    if jpegs and len(failed) == len(jpegs):
        raise BadUpload("The vision model couldn't read any of the pictures. Try again in a moment.", 502)
    return texts, sorted(failed)


def numbers(pages: list[int]) -> str:
    return ", ".join(str(n) for n in pages)


def has_picture(page) -> bool:
    """Does the PDF page contain a picture? A page with little text and no picture is just a short text page
    (a title, a divider, a contents list): there is nothing for the vision model to add."""
    try:
        return any(True for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))
    except Exception:
        return True  # can't tell: let the vision model look


def read_pdf(name: str, data: bytes, vision: VisionReader) -> Material:
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        reason = "is password-protected" if "password" in str(exc).lower() else "couldn't be opened as a PDF"
        raise BadUpload(f"{name} {reason}") from exc
    try:
        total = len(doc)
        if total > MAX_PDF_PAGES:
            raise BadUpload(f"{name} has {total} pages; the limit is {MAX_PDF_PAGES}. Upload a part of it.", 413)
        texts: dict[int, str] = {}
        pictures: dict[int, bytes] = {}
        skipped: list[int] = []
        for i in range(total):
            page = doc[i]
            textpage = page.get_textpage()
            try:
                text = clean(textpage.get_text_range())
                texts[i + 1] = text
                if len(text) < MIN_TEXT_CHARS and has_picture(page):
                    if len(pictures) < MAX_VISION_PAGES:
                        scale = min(2.0, IMAGE_MAX_SIDE / max(page.get_width(), page.get_height()))
                        pictures[i + 1] = to_jpeg(page.render(scale=scale).to_pil())
                    else:
                        skipped.append(i + 1)
            finally:
                textpage.close()
                page.close()
    finally:
        doc.close()

    warnings: list[str] = []
    read, failed = transcribe(pictures, vision) if pictures else ({}, [])
    pages = []
    for n in range(1, total + 1):
        if n in pictures:
            seen = read.get(n, "")
            pages.append(Page(number=n, text=seen or texts[n], via="vision" if seen else ("text" if texts[n] else "none")))
        else:
            pages.append(Page(number=n, text=texts[n], via="text" if texts[n] else "none"))
    if skipped:
        warnings.append(f"{len(skipped)} more pages look like pictures but only {MAX_VISION_PAGES} are read per upload "
                        f"(pages {numbers(skipped[:10])}{'...' if len(skipped) > 10 else ''} were skipped)")
    if failed:
        warnings.append(f"Couldn't read page{'s' if len(failed) > 1 else ''} {numbers(failed)}")
    return finish(Material(name=name, kind="pdf", pages=pages, warnings=warnings))


def read_photos(files: list[tuple[str, bytes]], vision: VisionReader) -> Material:
    jpegs: dict[int, bytes] = {}
    for n, (name, data) in enumerate(files, 1):
        try:
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > MAX_PIXELS:
                raise BadUpload(f"{name} is too large a picture ({img.width}x{img.height}); resize it first", 413)
            img.load()
        except Image.DecompressionBombError as exc:
            raise BadUpload(f"{name} is too large a picture; resize it first", 413) from exc
        except (UnidentifiedImageError, OSError) as exc:
            hint = " HEIC photos aren't supported; export them as JPG." if name.lower().endswith((".heic", ".heif")) else ""
            raise BadUpload(f"{name} isn't a PDF or a picture I can read (use PDF, JPG, PNG or WebP).{hint}", 415) from exc
        jpegs[n] = to_jpeg(img)
    read, failed = transcribe(jpegs, vision)
    pages = [Page(number=n, text=read.get(n, ""), via="vision" if read.get(n) else "none") for n in jpegs]
    warnings = [f"Couldn't read photo{'s' if len(failed) > 1 else ''} {numbers(failed)}"] if failed else []
    name = files[0][0] if len(files) == 1 else f"{len(files)} photos ({files[0][0]}, ...)"
    return finish(Material(name=name, kind="photos", pages=pages, warnings=warnings))


def finish(material: Material) -> Material:
    if not any(p.text for p in material.pages):
        raise BadUpload("I couldn't find any readable text in this upload.")
    material.contents = contents_pages(material.pages)
    material.chapters = detect_chapters(material.pages)
    return material


# --------------------------------------------------------------------------
# Chapters
# --------------------------------------------------------------------------
HEADING = re.compile(r"^\s*(chapter|unit|lesson|part|section)\s+(\d+|[ivxlcdm]+)\b[\s:.–—-]*(.*)$", re.I)
ASK_HEADING = re.compile(r"\b(chapter|unit|lesson|part|section)\s+(\d+|[ivxlcdm]+)\b", re.I)
ROMAN = re.compile(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", re.I)


def is_number(word: str) -> bool:
    """A chapter number: digits, or a well-formed roman numeral. Not words like 'civil' or 'mild'."""
    return word.isdigit() or (bool(word) and bool(ROMAN.fullmatch(word)))
MAX_CHAPTERS = 60


CONTENTS_TITLES = {"contents", "table of contents", "index"}


def top_lines(page: Page) -> list[str]:
    return [ln for ln in page.text.splitlines() if ln.strip()][:40]


def contents_pages(pages: list[Page]) -> list[int]:
    """Pages that list chapters or are titled Contents/Index: several different headings close together."""
    out = []
    for p in pages:
        lines = top_lines(p)
        headings = {(m[1].lower(), m[2].upper()) for ln in lines if (m := HEADING.match(ln)) and is_number(m[2])}
        if len(headings) >= 2 or (lines and lines[0].strip().casefold() in CONTENTS_TITLES):
            out.append(p.number)
    return out


def detect_chapters(pages: list[Page]) -> list[Chapter]:
    """Chapter starts found as a heading line near the top of a page. Contents pages and running headers
    (the same heading on every page) are ignored."""
    starts: dict[tuple[str, str], tuple[str, int]] = {}
    skip = set(contents_pages(pages))
    for p in pages:
        if p.number in skip:
            continue
        lines = top_lines(p)
        found = [(m, i) for i, ln in enumerate(lines) if (m := HEADING.match(ln)) and is_number(m[2])]
        top = next(((m, i) for m, i in found if i < 4), None)
        if top:
            m, _ = top
            key = (m[1].lower(), m[2].upper())
            title = f"{m[1].title()} {m[2].upper()}" + (f": {m[3].strip()}" if m[3].strip() else "")
            starts.setdefault(key, (title, p.number))
    ordered = sorted(starts.values(), key=lambda t: t[1])[:MAX_CHAPTERS]
    last = pages[-1].number if pages else 0
    return [Chapter(title=title, start=start, end=(ordered[i + 1][1] - 1 if i + 1 < len(ordered) else last))
            for i, (title, start) in enumerate(ordered)]


# --------------------------------------------------------------------------
# Finding the pages for a topic
# --------------------------------------------------------------------------
SPLIT = re.compile(r"[\s -⁯.,;:!?()\[\]{}\"'“”‘’«»/\\|<>=+*&^%$#@~`_–—-]+")
STOP = {"the", "a", "an", "of", "to", "in", "is", "are", "and", "or", "for", "on", "with", "as", "at", "by", "be", "it",
        "its", "this", "that", "what", "which", "how", "why", "about", "me", "my", "i", "please", "explain", "teach",
        "tell", "show", "learn", "study", "understand", "from", "into", "can", "you", "us", "we", "do", "does"}
PAGE_RANGE = re.compile(r"\bpp?(?:ages?)?\.?\s*(\d+)\s*(?:-|–|—|to|through)\s*(\d+)\b", re.I)
PAGE_ONE = re.compile(r"\bpage\s+(\d+)\b", re.I)
K1, B = 1.5, 0.75


def stem(word: str) -> str:
    """Plural endings only, so 'volcanoes' meets 'volcano' (and 'photosynthesis' stays itself)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("oes"):
        return word[:-2]                                   # volcanoes -> volcano
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]                                   # glasses, boxes, churches
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """Lower-cased words, lightly stemmed, without filler words. Splits only on spaces and punctuation, so
    scripts that use combining marks (Hindi, Arabic...) stay whole."""
    return [w for w in (stem(t) for t in SPLIT.split(text.casefold())) if len(w) >= 2 and w not in STOP]


def bm25(pages: list[Page], query: list[str]) -> list[float]:
    docs = [Counter(tokens(p.text)) for p in pages]
    lengths = [sum(d.values()) for d in docs]
    average = (sum(lengths) / len(docs)) or 1
    scores = [0.0] * len(docs)
    for term in set(query):
        df = sum(1 for d in docs if term in d)
        if not df:
            continue
        idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
        for i, d in enumerate(docs):
            tf = d.get(term, 0)
            if tf:
                scores[i] += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * lengths[i] / average))
    return scores


def spans(pages: list[int]) -> str:
    """[40, 41, 42, 47] -> '40-42, 47'"""
    out: list[str] = []
    for n in sorted(pages):
        if out and n == int(out[-1].split("-")[-1]) + 1:
            out[-1] = f"{out[-1].split('-')[0]}-{n}"
        else:
            out.append(str(n))
    return ", ".join(out)


def page_label(pages: list[int]) -> str:
    return ("page " if len(pages) == 1 else "pages ") + spans(pages)


def assemble(material: Material, ordered: list[int], how: str, label: str) -> Selection:
    """Text of `ordered` pages (most important first) up to the size of one lesson prompt, in page order."""
    by_number = {p.number: p for p in material.pages}
    chosen: list[int] = []
    used = 0
    for n in ordered:
        text = by_number[n].text
        if not text:
            continue
        if used + len(text) > EXCERPT_CHARS and chosen:
            continue
        chosen.append(n)
        used += len(text)
    chosen.sort()
    truncated = len(chosen) < len([n for n in ordered if by_number[n].text])
    text = "\n\n".join(f"[Page {n}]\n{by_number[n].text[:EXCERPT_CHARS]}" for n in chosen)
    return Selection(pages=chosen, how=how, label=label, text=text, truncated=truncated)


def chapter_of(material: Material, number: int) -> int | None:
    return next((i for i, c in enumerate(material.chapters) if c.start <= number <= c.end), None)


def select_pages(material: Material, query: str) -> Selection:
    query = query.strip()
    total = len(material.pages)

    span = PAGE_RANGE.search(query)
    one = None if span else PAGE_ONE.search(query)
    if span or one:
        a = int((span or one)[1])
        first, last = (min(a, int(span[2])), max(a, int(span[2]))) if span else (a, a)
        if first < 1 or last > total:
            raise NotInMaterial(f"This upload has {total} pages (1-{total}); you asked for {first}-{last}.")
        wanted = list(range(first, min(last, first + RANGE_PAGES - 1) + 1))
        return assemble(material, wanted, "pages", page_label(wanted))

    rest = query
    ask = next((a for a in ASK_HEADING.finditer(query) if is_number(a[2])), None)
    if ask:
        key = (ask[1].lower(), ask[2].upper())
        chapter = next((c for c in material.chapters
                        if (c.title.split(":")[0].split()[0].lower(), c.title.split(":")[0].split()[1]) == key), None)
        rest = query[:ask.start()] + " " + query[ask.end():]
        if chapter:
            wanted = list(range(chapter.start, min(chapter.end, chapter.start + RANGE_PAGES - 1) + 1))
            return assemble(material, wanted, "chapter", f"{chapter.title} ({page_label(wanted)})")
        if not tokens(rest):
            known = ", ".join(c.title.split(":")[0] for c in material.chapters[:8]) or "none found"
            raise NotInMaterial(f"I couldn't find {ask[1].title()} {ask[2]} in this upload's headings "
                                f"(headings found: {known}). Try a topic, or pages like 'pages 40-55'.")

    words = tokens(rest)
    if not words:
        raise NotInMaterial("Say what to study: a topic, a chapter like 'chapter 3', or pages like 'pages 40-55'.")
    scores = bm25(material.pages, words)
    for n in material.contents:
        scores[n - 1] = 0.0
    ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])
    if not ranked:
        raise NotInMaterial(f"I couldn't find “{rest.strip()}” in this upload. "
                            "Check the spelling, or try a chapter or a page range.")
    order: list[int] = []
    for i in ranked[:SEARCH_SEEDS]:
        for j in (i, i - 1, i + 1):  # the page itself, then its neighbours for context
            n = j + 1
            if not (0 <= j < total and n not in order and material.pages[j].text):
                continue
            if j != i and scores[j] <= 0:  # a neighbour that doesn't mention the topic only counts inside the same chapter
                here = chapter_of(material, i + 1)
                if here is None or here != chapter_of(material, n):
                    continue
            order.append(n)
    order = order[:EXCERPT_PAGES]
    return assemble(material, order, "search", f"pages matching “{rest.strip()}” ({spans(order)})")


# --------------------------------------------------------------------------
# The vision model
# --------------------------------------------------------------------------
TRANSCRIBE = (
    "Transcribe the text on this book page exactly as written, in reading order: headings, paragraphs, lists, "
    "captions. For a figure, table or diagram, add a short description in square brackets that includes every "
    "label you can read, for example [Diagram: ...]. Add nothing that is not on the page. If the page is blank, "
    "reply with nothing."
)


def model_vision_reader() -> VisionReader:
    """Reads a page image with the configured model (it must accept images, like the default does)."""
    from langchain_core.messages import HumanMessage

    from quiz_graph import _llm

    def read(jpeg: bytes) -> str:
        url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        message = HumanMessage(content=[{"type": "text", "text": TRANSCRIBE},
                                        {"type": "image_url", "image_url": {"url": url}}])
        out = _llm(temperature=0).invoke([message]).content
        if isinstance(out, list):
            out = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in out)
        return out

    return read
