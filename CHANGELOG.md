# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning where practical.

## [Unreleased]

## [1.7.0] - 2026-07-09
### Changed
- `Save + New` now resets the editor to blank defaults after saving.
- `Save + Duplicate` now keeps the saved label's values prefilled while starting a new unsaved draft.

## [1.6.0] - 2026-07-09
### Added
- New `0.5 Unit (2 per 1u)` and `0.3 Unit (3 per 1u)` label sizes.
- Equal-width icon/text columns for `0.3u` labels.

### Fixed
- 3MF/STEP export no longer extrudes the full label background as solid content geometry.
- Export now includes text labels by preferring contour geometry for tags with text.
- Exported text now meshes as solid fills instead of hollow outline glyphs.

### Changed
- `label_generator.scad` size mappings aligned with web export dimensions for `0.3u`, `0.5u`, `2u`, and `3u`.

## [1.5.0] - 2026-04-25
### Added
- `Save + Duplicate` action in the tag editor to save and continue as a new prefilled tag draft.
- In-app save success toast shown after `Save`, `Save + Duplicate`, and `Save + New`.

## [1.4.0] - 2026-04-25
### Added
- New `2 Lines (column)` text layout in Edit Tag, rendering two text columns side-by-side instead of stacked rows.

### Added
- Public project docs baseline:
  - `README.md`
  - `CONTRIBUTING.md`
  - `SECURITY.md`
  - `CODE_OF_CONDUCT.md`
  - `SUPPORT.md`
  - `LICENSE`
  - `docs/README.md`
- Community templates:
  - `.github/ISSUE_TEMPLATE/bug_report.md`
  - `.github/ISSUE_TEMPLATE/feature_request.md`
  - `.github/PULL_REQUEST_TEMPLATE.md`
- Repository standards files:
  - `.editorconfig`
  - `.gitattributes`

### Changed
- Dashboard UX improvements (sticky top action dock + fixed bottom batch export dock).
- Batch export controls and status presentation.
- Tag editor UX improvements (compact selectors, inline zone editor).

### Removed
- Legacy scratch/temporary files from repository root.
