import pytest
from books import book_pages, chapter_start

from readcue.pagecheck import check_range

PAGES = {i: text for i, text in enumerate(book_pages(), 1)}
COUNT = len(PAGES)


def check(start, end, number=2, pages=None):
    return check_range(pages or PAGES, COUNT, start, end, number)


def cards(result):
    return {c["role"]: c for c in result["cards"]}


def test_a_correct_range_shows_the_pages_around_it_with_reassuring_notes():
    start, end = chapter_start(2), chapter_start(3) - 1
    result = check(start, end)
    c = cards(result)
    assert (c["before"]["page"], c["first"]["page"], c["last"]["page"], c["after"]["page"]) == (
        start - 1,
        start,
        end,
        end + 1,
    )
    assert (c["first"]["tone"], c["first"]["note"]) == ("ok", "Opens Chapter 2.")
    assert (c["after"]["tone"], c["after"]["note"]) == ("ok", "Opens Chapter 3: looks like the next chapter.")
    assert c["before"]["tone"] is None
    assert result["pages"] == 6 and result["words"] > 50 and result["warnings"] == []
    assert c["first"]["head"][0] == "Chapter 2 The Cell"


def test_starting_a_page_late_points_out_the_heading_on_the_page_before():
    result = check(chapter_start(2) + 1, chapter_start(3) - 1)
    c = cards(result)
    assert c["before"]["tone"] == "warn" and "Chapter 2's heading is on this page" in c["before"]["note"]
    assert c["first"]["tone"] is None and "No “Chapter” heading" in c["first"]["note"]


def test_ending_a_page_early_says_the_chapter_may_be_cut_short():
    c = cards(check(chapter_start(2), chapter_start(3) - 2))
    assert c["after"]["tone"] == "warn" and "cut short" in c["after"]["note"]


def test_running_into_the_next_chapter_is_flagged():
    result = check(chapter_start(2), chapter_start(3) + 1)
    assert any(f"Page {chapter_start(3)} opens Chapter 3" in w for w in result["warnings"])


def test_starting_on_the_wrong_chapter_is_flagged():
    first = cards(check(chapter_start(3), chapter_start(4) - 1, number=2))["first"]
    assert first["tone"] == "warn" and "opens Chapter 3, not Chapter 2" in first["note"]


def test_the_last_page_of_the_book_has_no_page_after_it():
    result = check(chapter_start(5), COUNT)
    after = cards(result)["after"]
    assert after["page"] is None and "end of the book" in after["note"]


def test_the_first_page_of_the_book_has_no_page_before_it():
    assert "before" not in cards(check(1, 3, number=1))


def test_before_a_back_matter_page_is_recognised():
    glossary = COUNT - 1
    after = cards(check(chapter_start(5), glossary - 1, number=5))["after"]
    assert after["tone"] == "ok" and "Back matter" in after["note"]


def test_pages_without_readable_text_are_called_out():
    pages = dict(PAGES)
    pages[chapter_start(2) + 2] = ""
    pages[chapter_start(2) + 3] = "  "
    result = check(chapter_start(2), chapter_start(3) - 1, pages=pages)
    assert any("2 pages in this range have no readable text" in w for w in result["warnings"])


def test_a_blank_first_page_is_flagged():
    pages = dict(PAGES)
    pages[chapter_start(2)] = ""
    assert cards(check(chapter_start(2), chapter_start(3) - 1, pages=pages))["first"]["tone"] == "warn"


def test_long_pages_are_cut_for_the_whole_page_view():
    pages = dict(PAGES)
    pages[chapter_start(2)] = "word " * 2000
    first = cards(check(chapter_start(2), chapter_start(3) - 1, pages=pages))["first"]
    assert first["truncated"] is True and len(first["text"]) == 3500


@pytest.mark.parametrize("start,end", [(0, 5), (5, 3), (3, 999)])
def test_ranges_outside_the_book_are_rejected(start, end):
    with pytest.raises(ValueError, match="don't fit"):
        check(start, end)
