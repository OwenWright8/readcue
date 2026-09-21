SUMMARY_SYSTEM = (
    "You are a study assistant that writes accurate summaries of university textbook chapters. "
    "Use only the text you are given; never add facts from outside it. "
    "Reply with a single JSON object and nothing else."
)

_SUMMARY_KEYS = """Return JSON with exactly these keys:
- "overview": string. {overview_length} Explain what the chapter covers and how its ideas connect. Separate paragraphs with a blank line.
- "key_points": array of {points} strings. Each is one concise takeaway a student should remember.
- "definitions": array of objects {{"term": string, "definition": string}} for every important term the text defines or introduces (bolded or italicised key terms, glossary-style terms). Each definition is 1-2 sentences and stays faithful to the text."""


def summary_prompt(course: str, number: int, title: str, text: str) -> str:
    heading = f"Chapter {number}" + (f": {title}" if title else "")
    keys = _SUMMARY_KEYS.format(overview_length="2-4 paragraphs (about 250-400 words).", points="6-12")
    return f"Course: {course}\n{heading}\n\nSummarize this chapter for a student who needs to read it.\n{keys}\n\n<chapter>\n{text}\n</chapter>"


def part_prompt(course: str, number: int, title: str, index: int, total: int, text: str) -> str:
    heading = f"Chapter {number}" + (f": {title}" if title else "")
    keys = _SUMMARY_KEYS.format(overview_length="1-2 paragraphs.", points="4-8")
    return (
        f"Course: {course}\n{heading}\n\nThis is part {index} of {total} of the chapter. "
        f"Summarize only this part.\n{keys}\n\n<chapter_part>\n{text}\n</chapter_part>"
    )


def combine_prompt(course: str, number: int, title: str, parts_json: str) -> str:
    heading = f"Chapter {number}" + (f": {title}" if title else "")
    return (
        f"Course: {course}\n{heading}\n\nBelow are summaries of consecutive parts of one chapter. "
        "Combine them into a single summary of the whole chapter.\n"
        "Return JSON with exactly these keys:\n"
        '- "overview": string. 2-4 paragraphs (about 250-400 words) covering the whole chapter in order. '
        "Separate paragraphs with a blank line.\n"
        '- "key_points": array of 6-12 strings, the most important takeaways across all parts, without duplicates.\n\n'
        f"<parts>\n{parts_json}\n</parts>"
    )


SYLLABUS_SYSTEM = (
    "You extract reading schedules from course syllabi. Reply with a single JSON object and nothing else."
)


def syllabus_prompt(text: str, today: str) -> str:
    return f"""Today's date is {today}.

Extract the textbook chapter reading schedule from this syllabus.

Return JSON: {{"readings": [{{"chapter": integer, "title": string, "due_date": "YYYY-MM-DD"}}]}}

Rules:
- Include only textbook chapter readings. Skip exams, assignments, articles and other readings.
- "due_date" is the date the reading must be finished by. If the syllabus says to read before a class meeting, use that meeting's date. If only a week is given, use the last day of that week.
- When one line assigns several chapters (for example "Chapters 4-5"), emit one entry per chapter with the same date.
- If a date has no year, use the term dates in the syllabus to pick it; otherwise pick the year that puts it in the current or upcoming term.
- "title" is the chapter's title or topic if the syllabus gives one, otherwise an empty string.
- If no chapter readings are listed, return {{"readings": []}}.

<syllabus>
{text}
</syllabus>"""
