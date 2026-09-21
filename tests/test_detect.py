import pytest
from books import PAGES_PER_CHAPTER, TITLES, book_pages, chapter_start

from readcue.detect import chapter_marks, detect_chapters, is_contents_page

WANTED = dict(TITLES)


def by_number(found):
    return {f.number: f for f in found}


def test_finds_each_chapter_and_ends_it_before_the_next_opens():
    found = by_number(detect_chapters(book_pages(), WANTED))
    for n in (1, 2, 3, 4):
        assert found[n].start == chapter_start(n)
        assert found[n].end == chapter_start(n + 1) - 1
        assert found[n].pages == PAGES_PER_CHAPTER
        assert found[n].source == "heading"
    assert "Chapter 2 The Cell" in found[2].snippet


def test_last_chapter_ends_where_back_matter_begins():
    found = by_number(detect_chapters(book_pages(), WANTED))
    glossary_page = chapter_start(5) + PAGES_PER_CHAPTER  # the page after chapter 5's last
    assert found[5].end == glossary_page - 1
    assert not found[5].warnings


def test_only_the_wanted_chapters_are_returned_but_all_chapters_set_the_boundaries():
    found = detect_chapters(book_pages(), {2: "The Cell"})
    assert [f.number for f in found] == [2]
    assert found[0].end == chapter_start(3) - 1  # chapter 3 isn't wanted, but its heading ends chapter 2


def test_the_contents_page_is_not_mistaken_for_chapter_openers():
    pages = book_pages()
    assert is_contents_page(pages[1]) and not is_contents_page(pages[2])
    assert detect_chapters(pages, {1: "Introduction"})[0].start == chapter_start(
        1
    )  # not page 2, the contents


def test_a_see_chapter_reference_on_an_earlier_page_is_ignored():
    pages = book_pages()
    cross_reference_page = chapter_start(1) + 2  # "Chapter 3 is covered later" sits atop this page
    assert 3 in chapter_marks(pages[cross_reference_page - 1], set())
    assert by_number(detect_chapters(pages, WANTED))[3].start == chapter_start(3)


def test_bookmarks_are_used_when_the_pdf_has_them():
    outline = [(f"Chapter {n}: {t}", chapter_start(n)) for n, t in TITLES.items()]
    found = by_number(detect_chapters(book_pages(), WANTED, outline))
    assert {f.source for f in found.values()} == {"bookmark"}
    assert found[3].start == chapter_start(3) and found[3].end == chapter_start(4) - 1


def test_bookmarks_without_the_word_chapter_still_work():
    outline = [("7. Cell Membranes", 10), ("8. Photosynthesis", 20)]
    pages = ["filler text on an ordinary page of the book"] * 30
    found = by_number(detect_chapters(pages, {7: "", 8: ""}, outline))
    assert (found[7].start, found[7].end) == (10, 19)


def test_falls_back_to_the_syllabus_title_when_headings_dont_say_chapter():
    pages = ["Cover"] + ["Some filler text for an ordinary page"] * 4
    pages += ["Cell Membranes\nThe boundary of every cell is a membrane made of lipids"]
    pages += ["Running text about membranes and their proteins for a while"] * 3
    pages += ["Photosynthesis\nHow plants capture light and turn it into sugar"]
    pages += ["Running text about light reactions and the Calvin cycle for a while"] * 2
    found = by_number(detect_chapters(pages, {7: "Cell Membranes", 8: "Photosynthesis"}))
    assert (found[7].start, found[7].end, found[7].source) == (6, 9, "title")
    assert found[8].start == 10


def test_numbered_headings_count_only_for_chapters_on_the_schedule():
    pages = ["filler text for a page in the middle of the book"] * 3
    pages[1] = "7 Cell Membranes\nThe boundary of every cell"
    assert detect_chapters(pages, {7: ""})[0].start == 2
    assert chapter_marks(pages[1], set()) == set()  # "7 Cell..." alone isn't enough evidence


def test_a_standalone_page_number_is_not_a_chapter_heading():
    assert chapter_marks("142\nSome running text that follows a bare page number", {142}) == set()


def test_roman_numeral_chapters():
    pages = ["filler text on an ordinary page of the book"] * 3 + [
        "CHAPTER VII\nCell Membranes and transport"
    ]
    assert detect_chapters(pages, {7: ""})[0].start == 4


def test_a_missing_chapter_comes_back_unlocated_with_a_warning():
    found = detect_chapters(book_pages(), {9: "Ecology"})[0]
    assert found.start is None and found.end is None
    assert any("opening page" in w for w in found.warnings)


def test_an_end_that_cant_be_found_defaults_to_the_last_page_with_a_warning():
    pages = ["Chapter 1 Intro\nOpening"] + ["Running text about the topic for a good while"] * 5
    found = detect_chapters(pages, {1: "Intro"})[0]
    assert (found.start, found.end) == (1, 6)
    assert any("ends" in w for w in found.warnings)


def test_an_unusually_long_chapter_is_flagged():
    pages = (
        ["Chapter 1 Intro\nOpening"]
        + ["Running text about the topic for a good while"] * 200
        + ["Index\nA 1"]
    )
    found = detect_chapters(pages, {1: "Intro"})[0]
    assert found.pages == 201 and any("unusually long" in w for w in found.warnings)


@pytest.mark.parametrize(
    "line", ["Chapter 7", "CHAPTER 7: Cell Membranes", "Ch. 7 Cell Membranes", "142  Chapter 7 Cells"]
)
def test_common_heading_shapes(line):
    assert 7 in chapter_marks(f"{line}\nBody text follows the heading here", set())
