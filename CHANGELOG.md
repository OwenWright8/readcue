# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-09-20

First release.

### Added
- **Syllabus import.** Upload a syllabus (PDF, Word, text, scans or photos) or paste it; the AI extracts every
  chapter reading and due date, and you review and edit them before they are saved.
- **Chapter summaries.** Upload chapters one at a time (PDF with optional page range, Word, text, scans, photos,
  or pasted text). Each gets an overview, key points and important definitions. Chapters longer than the model's
  context window are summarized in parts and combined without dropping definitions.
- **Summarize now or schedule.** Per course: write each summary as soon as the chapter is added, or 3 days before
  it is due (configurable) so a syllabus change never wastes a summary. A per-chapter "Summarize now" override.
- **Pushover notifications.** A "summary ready" ping when a summary completes, and a reminder the day before each
  chapter is due, both with an "Open summary" link. Never duplicated, and re-armed if a due date moves.
- **OCR** for scanned PDFs and photos of pages (Tesseract and Poppler, included in the image).
- **Claude or Ollama.** Claude Sonnet by default; a self-hosted Ollama model, bundled or external, with one setting.
- **Web UI** with a dashboard, progress tracking per chapter, a reading view with a searchable glossary, light and
  dark themes, and a phone-friendly layout. No external assets.
- **Self-hosting.** Docker image (amd64 and arm64) and Docker Compose file with an optional bundled Ollama.

### Security and operations
- Refuses to start when reachable beyond localhost without `READCUE_PASSWORD`.
- Optional HTTP basic auth, cross-site POST protection, a strict Content-Security-Policy and other security
  headers, `Cache-Control: no-store` on pages, and secrets kept out of logs.
- Runs as a non-root user with all capabilities dropped; health check covers the database and background threads.
- `readcue backup` writes a consistent copy of the database while the app is running.
- Limits on upload size, extracted text, Word-document expansion and concurrent OCR.

[1.0.0]: https://github.com/OwenWright8/readcue/releases/tag/v1.0.0
