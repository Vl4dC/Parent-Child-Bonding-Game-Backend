"""Real PDFs and pictures for the upload tests, built in memory."""

import io

from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader, PdfWriter

BOOK = [
    "Contents\nChapter 1 Plants and light\nChapter 2 Volcanoes and lava\nIndex at the back of the book",
    "Chapter 1: Plants and light\nPlants make their own food by photosynthesis. They take in light, water and "
    "carbon dioxide and make sugar and oxygen.",
    "Chlorophyll is the green substance in leaves that absorbs sunlight for photosynthesis. More light means the "
    "plant makes sugar faster until it has all the water it can use.",
    "Chapter 2: Volcanoes and lava\nA volcano erupts when magma rises through a crack in the crust. Magma that "
    "reaches the surface is called lava.",
    "Volcanoes form where tectonic plates meet. Lava cools into rock, and layers of rock build up a mountain "
    "over many eruptions.",
    "Review questions and answers for the whole book are printed on this page so students can check their work.",
]


def make_pdf(pages: list[str] = BOOK) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 7, text)
    return bytes(pdf.output())


def make_png(width: int = 600, height: int = 800) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 250)).save(out, "PNG")
    return out.getvalue()


def make_scanned_pdf(pages: int) -> bytes:
    """A PDF whose pages are pictures only (no text layer). Page n is a flat grey of level 100 + 20n, so a
    fake vision reader can tell the pages apart with page_id()."""
    images = [Image.new("RGB", (600, 800), (120 + 20 * i,) * 3) for i in range(pages)]
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:])
    return out.getvalue()


def make_mixed_pdf() -> bytes:
    """Page 1 has text, page 2 is only a picture."""
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0, 7, "This page has a real text layer, long enough to count as text and not as a picture."
    )
    pdf.add_page()
    pdf.image(io.BytesIO(make_png(400, 300)), x=10, y=10, w=100)
    return bytes(pdf.output())


def encrypt(pdf: bytes, password: str = "secret") -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf)))
    writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_width(jpeg: bytes) -> int:
    """Width of the picture a fake vision reader was handed: identifies which page it is."""
    return Image.open(io.BytesIO(jpeg)).width


def page_id(jpeg: bytes) -> int:
    """Which page of make_scanned_pdf() a fake vision reader was handed (1-based), from its grey level."""
    level = Image.open(io.BytesIO(jpeg)).convert("L").getpixel((50, 50))
    return round((level - 100) / 20)
