import pytest

import materials as mt
from files import BOOK, encrypt, make_mixed_pdf, make_pdf, make_png, make_scanned_pdf, page_id, page_width


def no_vision(_jpeg):
    raise AssertionError("a text PDF must not need the vision model")


def by_width(jpeg):
    return f"Scanned page {page_width(jpeg) - 599} about volcanoes and lava flows"


def by_page(jpeg):
    return f"Scanned page {page_id(jpeg)} about volcanoes and lava flows"


def book():
    return mt.read_files([("book.pdf", make_pdf())], no_vision)


# ---------------------------------------------------------------- reading
def test_text_pdf_is_read_locally_page_by_page():
    m = book()
    assert m.kind == "pdf" and m.name == "book.pdf" and len(m.pages) == 6
    assert all(p.via == "text" for p in m.pages)
    assert "photosynthesis" in m.pages[1].text and m.pages[4].number == 5
    info = m.info()
    assert (info.page_count, info.text_pages, info.vision_pages, info.blank_pages) == (6, 6, 0, 0)


def test_chapters_skip_the_contents_page_and_use_the_real_start():
    chapters = book().chapters
    assert [(c.title, c.start, c.end) for c in chapters] == [
        ("Chapter 1: Plants and light", 2, 3), ("Chapter 2: Volcanoes and lava", 4, 6)]


def test_running_headers_do_not_move_a_chapter_start():
    pages = ["Chapter 1: Plants\nFirst page of the chapter with plenty of words in it to be read.",
             "Chapter 1: Plants\nSecond page, same running header at the top, still plenty of words here.",
             "Chapter 2: Rocks\nA new chapter begins on this page with plenty of words in it as well."]
    chapters = mt.read_files([("b.pdf", make_pdf(pages))], no_vision).chapters
    assert [(c.title.split(":")[0], c.start, c.end) for c in chapters] == [("Chapter 1", 1, 2), ("Chapter 2", 3, 3)]


def test_words_that_look_like_roman_numerals_are_not_chapters():
    assert [mt.is_number(w) for w in ("3", "IV", "xii", "civil", "mild", "di", "")] == [True, True, True, False, False, True, False]
    pages = [mt.Page(number=1, text="Section civil law applies to disputes between people.", via="text")]
    assert mt.detect_chapters(pages) == []


def test_scanned_pdf_pages_are_read_by_the_vision_model():
    m = mt.read_files([("scan.pdf", make_scanned_pdf(3))], by_page)
    assert [p.via for p in m.pages] == ["vision"] * 3
    assert [p.text for p in m.pages] == [f"Scanned page {i} about volcanoes and lava flows" for i in (1, 2, 3)]
    assert m.info().vision_pages == 3


def test_a_pdf_can_mix_text_pages_and_picture_pages():
    m = mt.read_files([("mixed.pdf", make_mixed_pdf())], by_width)
    assert [p.via for p in m.pages] == ["text", "vision"]
    assert m.pages[0].text.startswith("This page has a real text layer")


def test_vision_reads_only_as_many_pictures_as_the_cap_allows(monkeypatch):
    monkeypatch.setattr(mt, "MAX_VISION_PAGES", 2)
    m = mt.read_files([("scan.pdf", make_scanned_pdf(4))], by_width)
    assert sorted(p.number for p in m.pages if p.via == "vision") == [1, 2]
    assert "2 more pages look like pictures" in m.warnings[0]


def test_pages_the_vision_model_fails_on_are_reported_not_fatal():
    def flaky(jpeg):
        if page_id(jpeg) == 2:
            raise RuntimeError("model hiccup")
        return "Readable text on this picture page"

    m = mt.read_files([("scan.pdf", make_scanned_pdf(3))], flaky)
    assert [p.via for p in m.pages] == ["vision", "none", "vision"]
    assert m.warnings == ["Couldn't read page 2"]


def test_when_vision_fails_everywhere_the_upload_is_refused_with_a_502():
    def down(_jpeg):
        raise RuntimeError("no network")

    with pytest.raises(mt.BadUpload) as err:
        mt.read_files([("scan.pdf", make_scanned_pdf(2))], down)
    assert err.value.status == 502


def test_photos_become_pages_in_upload_order():
    files = [("a.png", make_png(600, 800)), ("b.png", make_png(601, 800))]
    m = mt.read_files(files, by_width)
    assert m.kind == "photos" and m.name.startswith("2 photos (a.png")
    assert [p.text for p in m.pages] == ["Scanned page 1 about volcanoes and lava flows",
                                         "Scanned page 2 about volcanoes and lava flows"]


def test_large_photos_are_shrunk_before_they_reach_the_model():
    seen = []
    mt.read_files([("big.png", make_png(4000, 3000))], lambda jpeg: seen.append(page_width(jpeg)) or "Text on the picture")
    assert seen == [mt.IMAGE_MAX_SIDE]


def test_blank_pages_make_no_text_and_an_all_blank_upload_is_refused():
    with pytest.raises(mt.BadUpload, match="readable text"):
        mt.read_files([("blank.png", make_png())], lambda _j: "")


# ---------------------------------------------------------------- refusing bad uploads
@pytest.mark.parametrize("files, status, message", [
    ([], 422, "Upload a PDF"),
    ([("notes.txt", b"just some text")], 415, "isn't a PDF or a picture"),
    ([("pic.heic", b"\x00\x00\x00 ftypheic")], 415, "HEIC"),
    ([("empty.pdf", b"")], 422, "is empty"),
    ([("a.pdf", make_pdf()), ("b.pdf", make_pdf())], 422, "one PDF at a time"),
    ([("a.pdf", make_pdf()), ("p.png", make_png())], 422, "one PDF at a time"),
    ([("broken.pdf", b"%PDF-1.4 this is not really a pdf")], 422, "couldn't be opened"),
    ([("locked.pdf", encrypt(make_pdf()))], 422, "password-protected"),
])
def test_bad_uploads_get_a_clear_reason(files, status, message):
    with pytest.raises(mt.BadUpload, match=message) as err:
        mt.read_files(files, by_width)
    assert err.value.status == status


def test_size_and_count_limits(monkeypatch):
    monkeypatch.setattr(mt, "MAX_FILE_BYTES", 1000)
    with pytest.raises(mt.BadUpload, match="larger than") as err:
        mt.read_files([("big.png", b"x" * 1001)], by_width)
    assert err.value.status == 413
    monkeypatch.setattr(mt, "MAX_FILE_BYTES", 10_000_000)
    monkeypatch.setattr(mt, "MAX_FILES", 2)
    with pytest.raises(mt.BadUpload, match="at most 2 files"):
        mt.read_files([("a.png", make_png()), ("b.png", make_png()), ("c.png", make_png())], by_width)
    monkeypatch.setattr(mt, "MAX_FILES", 20)
    monkeypatch.setattr(mt, "MAX_TOTAL_BYTES", 50)
    with pytest.raises(mt.BadUpload, match="add up to more than"):
        mt.read_files([("a.png", make_png()), ("b.png", make_png())], by_width)


def test_a_pdf_with_too_many_pages_is_refused(monkeypatch):
    monkeypatch.setattr(mt, "MAX_PDF_PAGES", 3)
    with pytest.raises(mt.BadUpload, match="has 6 pages") as err:
        mt.read_files([("book.pdf", make_pdf())], no_vision)
    assert err.value.status == 413


# ---------------------------------------------------------------- finding the pages for a request
def test_page_ranges_and_single_pages():
    m = book()
    sel = mt.select_pages(m, "pages 2-3")
    assert (sel.pages, sel.how, sel.label) == ([2, 3], "pages", "pages 2-3")
    assert sel.text.startswith("[Page 2]\nChapter 1") and "[Page 3]" in sel.text
    assert mt.select_pages(m, "explain p. 4 to 5 please").pages == [4, 5]
    assert mt.select_pages(m, "page 5").pages == [5]
    assert mt.select_pages(m, "pages 5-4").pages == [4, 5]  # backwards is fine


def test_page_requests_outside_the_book_say_how_many_pages_there_are():
    with pytest.raises(mt.NotInMaterial, match=r"has 6 pages \(1-6\)"):
        mt.select_pages(book(), "pages 5-9")
    with pytest.raises(mt.NotInMaterial, match="has 6 pages"):
        mt.select_pages(book(), "page 0")


def test_a_chapter_by_name():
    sel = mt.select_pages(book(), "chapter 2")
    assert (sel.pages, sel.how) == ([4, 5, 6], "chapter")
    assert sel.label == "Chapter 2: Volcanoes and lava (pages 4-6)"
    assert mt.select_pages(book(), "Teach me Chapter 1 please").pages == [2, 3]


def test_a_chapter_the_book_doesnt_have_lists_the_ones_it_has():
    with pytest.raises(mt.NotInMaterial, match="Chapter 9.*headings found: Chapter 1, Chapter 2"):
        mt.select_pages(book(), "chapter 9")


def test_topic_search_finds_the_right_pages_and_their_neighbours():
    sel = mt.select_pages(book(), "photosynthesis")
    assert sel.how == "search" and 2 in sel.pages and 3 in sel.pages
    assert sel.pages == [2, 3]  # the contents page and the next chapter's first page are not "neighbours"
    assert "olcano" not in sel.text
    volcano = mt.select_pages(book(), "how do volcanoes erupt?")
    assert volcano.pages == [4, 5, 6] and "photosynthesis" not in volcano.text  # not the contents page; 6 is chapter 2's last page


def test_search_matches_plurals_and_fillers_are_ignored():
    assert mt.tokens("The volcanoes are erupting!") == ["volcano", "erupting"]
    assert mt.tokens("boxes churches stories analysis") == ["box", "church", "story", "analysis"]
    assert {4, 5} <= set(mt.select_pages(book(), "please explain volcano to me").pages)


def test_a_topic_the_book_never_mentions_is_not_guessed_at():
    with pytest.raises(mt.NotInMaterial, match="couldn't find .quantum entanglement."):
        mt.select_pages(book(), "quantum entanglement")


def test_a_request_with_nothing_to_search_for():
    with pytest.raises(mt.NotInMaterial, match="Say what to study"):
        mt.select_pages(book(), "please explain it to me")


def test_the_excerpt_is_capped_and_says_so(monkeypatch):
    monkeypatch.setattr(mt, "EXCERPT_CHARS", 300)
    sel = mt.select_pages(book(), "chapter 2")
    assert sel.truncated is True and len(sel.pages) < 3 and sel.pages[0] == 4
    assert mt.select_pages(book(), "page 2").truncated is False


def test_bm25_prefers_the_page_where_a_rare_word_is_concentrated():
    pages = [mt.Page(number=i + 1, text=t, via="text") for i, t in enumerate([
        "the cell wall protects the cell and the cell membrane controls what enters",
        "mitochondria make energy for the cell, mitochondria mitochondria",
        "a long page about the weather rain clouds and wind and the seasons"])]
    scores = mt.bm25(pages, mt.tokens("mitochondria"))
    assert scores[1] > 0 and scores[0] == 0 and scores[2] == 0


def test_other_scripts_are_searched_too():
    pages = [mt.Page(number=1, text="प्रकाश संश्लेषण में पौधे सूर्य के प्रकाश से भोजन बनाते हैं", via="text"),
             mt.Page(number=2, text="ज्वालामुखी तब फटता है जब लावा ऊपर आता है", via="text")]
    m = mt.Material(name="hindi", kind="pdf", pages=pages)
    assert mt.tokens("प्रकाश संश्लेषण") == ["प्रकाश", "संश्लेषण"]
    assert mt.select_pages(m, "संश्लेषण").pages == [1]
    assert mt.select_pages(m, "ज्वालामुखी").pages == [2]


def test_span_formatting():
    assert mt.spans([40, 41, 42, 47]) == "40-42, 47" and mt.spans([3]) == "3" and mt.spans([5, 4]) == "4-5"


def test_model_vision_reader_sends_the_picture_and_returns_the_text(monkeypatch):
    import quiz_graph

    seen = {}

    class FakeModel:
        def invoke(self, messages):
            seen["parts"] = messages[0].content
            return type("R", (), {"content": [{"type": "text", "text": "Page text"}, {"type": "text", "text": " more"}]})()

    monkeypatch.setattr(quiz_graph, "_llm", lambda model=None, temperature=1.0: seen.update(temp=temperature) or FakeModel())
    assert mt.model_vision_reader()(b"jpegbytes") == "Page text more"
    assert seen["temp"] == 0  # transcription should not be creative
    assert seen["parts"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "Transcribe the text" in seen["parts"][0]["text"]


def test_sample_book_matches_what_the_tests_assume():
    assert len(BOOK) == 6


def test_contents_and_index_pages_are_recognised_and_never_searched():
    m = book()
    assert m.contents == [1]
    with pytest.raises(mt.NotInMaterial):
        mt.select_pages(m, "index")  # the word only appears on the contents page (and as a heading there)
    titled = [mt.Page(number=1, text="Index\nvolcano 4, 9\nlava 4, 12", via="text"),
              mt.Page(number=2, text="Volcanoes are mountains that erupt hot lava onto the ground", via="text")]
    assert mt.contents_pages(titled) == [1]


def test_labels_say_page_for_one_page_and_pages_for_several():
    m = book()
    assert mt.select_pages(m, "page 5").label == "page 5"
    assert mt.select_pages(m, "pages 5-6").label == "pages 5-6"
    one_page_chapter = mt.Material(name="b", kind="pdf", pages=m.pages[:3], chapters=[mt.Chapter(title="Chapter 2: Soil", start=3, end=3)])
    assert mt.select_pages(one_page_chapter, "chapter 2").label == "Chapter 2: Soil (page 3)"


def test_short_text_pages_are_not_sent_to_the_vision_model():
    short = ["Contents\nChapter 1 Light", "Chapter 1: Light\nPlants use light to make food, and the class measured it every day."]
    m = mt.read_files([("b.pdf", make_pdf(short))], no_vision)  # no_vision fails the test if it is called
    assert [p.via for p in m.pages] == ["text", "text"] and m.pages[0].text.startswith("Contents")
