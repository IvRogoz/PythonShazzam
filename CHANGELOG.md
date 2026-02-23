# Changelog

All notable changes to this project are documented in this file.

## [1.1.0] - 2026-02-23

### Added
- New Tkinter desktop GUI (`gui.py`) for scanning and processing MP3 files.
- Windows launcher script (`run_gui.bat`) that boots/uses a local Python 3.12 virtual environment.
- Row-based file list with cover thumbnail, filename, metadata details, and live status.
- Right-click row menu actions: `Open File`, `Open Folder`, `Rescan Item`.
- Auto-apply toggle with deferred workflow:
  - `Auto apply changes` ON: writes tags/renames during processing.
  - `Auto apply changes` OFF: stores detected results as pending and enables `Apply Pending`.
- Pending-mode visual preview of Shazam cover art before writing to disk.
- Dark mode as default startup theme with runtime toggle to normal mode.

### Changed
- Console output in `music.py` now uses ASCII-safe symbols and safer stream encoding behavior for Windows terminals.
- `README.md` expanded with GUI usage and executable build steps.
- `requirements.txt` updated to include `Pillow` and Python 3.13+ `audioop-lts` compatibility.

### Fixed
- Windows Unicode console crashes caused by unsupported glyph output.
- Python 3.14 instability guidance documented; recommended Python version remains 3.12/3.13.
