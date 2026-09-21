# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- `docker-compose.yml` now pulls the published image, so a NAS or server needs only that file and a `.env`, with no
  source checkout. The old build-from-source setup moved to the opt-in `docker-compose.build.yml` override. The
  `docker-compose.prebuilt.yml` override is gone.
- Every setting is passed through the compose `environment:` block instead of `env_file`, so it works on older
  Compose versions and in NAS/Portainer UIs.

### Added
- `READCUE_DATA` (keep the data in a folder such as a NAS share) and `READCUE_UID`/`READCUE_GID` (run as that
  folder's owner) in the compose file.
- A NAS section in `docs/deployment.md`, and CI that starts the compose file itself, including a NAS-style bind mount
  with a custom user.

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
