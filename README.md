<div align="center">
  <a href="https://github.com/ViminioSM/MedStitch">
    <img alt="MedStitch Logo" width="200" height="200" src="https://github.com/ViminioSM/MedStitch/raw/dev/assets/SmartStitchLogo.png">
  </a>
  <h1>MedStitch</h1>
  <p>Fast webtoon/manhwa/manhua stitcher and panel slicer for editors and scanlation workflows.</p>
  <p>
    <a href="https://github.com/ViminioSM/MedStitch/releases/latest"><img src="https://img.shields.io/github/v/release/ViminioSM/MedStitch" alt="Latest Release"></a>
    <a href="https://github.com/ViminioSM/MedStitch/actions/workflows/build.yml"><img src="https://img.shields.io/github/actions/workflow/status/ViminioSM/MedStitch/build.yml" alt="Build Status"></a>
    <a href="https://github.com/ViminioSM/MedStitch/releases"><img src="https://img.shields.io/github/downloads/ViminioSM/MedStitch/total" alt="Downloads"></a>
    <a href="https://github.com/ViminioSM/MedStitch/blob/dev/LICENSE"><img src="https://img.shields.io/github/license/ViminioSM/MedStitch" alt="License"></a>
  </p>
</div>

## Overview
MedStitch combines multiple source images into long pages and slices them into reader-friendly panels.

Main goals:
- Preserve quality.
- Avoid bad cuts through text/art as much as possible.
- Keep workflow simple and fast.
- Support both GUI and CLI usage.

## Current Features

### GUI
- Folder-based stitching and slicing.
- Detector modes:
  - Smart pixel comparison.
  - Direct fixed slicing.
- Output formats: `.png`, `.jpg`, `.webp`, `.bmp`, `.psd`, `.tiff`, `.tga`.
- Width enforcement modes:
  - None.
  - Automatic (smallest width).
  - Custom width.
- Settings profiles.
- Optional post-process command with placeholders:
  - `[stitched]` for stitched output path.
  - `[processed]` for post-process output path.
- Optional ComicZip integration.
- Windows context menu integration.
- Built-in update check and compiled-app self-update from GitHub Releases.

### Watermark System
- Full-page watermark mode for uniform blocks.
- Overlay watermark mode with position/opacity/scale controls.
- Header and footer image insertion.
- Context-menu quick toggle for watermark state (with state restore behavior).

### Console
- CLI pipeline for batch/headless processing.
- Core detector and slicing options available through arguments.

## Quick Start (Windows, GUI)
1. Download latest release from GitHub Releases.
2. Extract the package.
3. Run `SmartStitch.exe`.
4. Set input folder.
5. Adjust output/detector settings if needed.
6. Click start.

## Quick Start (Source)
1. Install Python 3.11+.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run GUI:

```bash
python SmartStitchGUI.py
```

4. Or run Console:

```bash
python SmartStitchConsole.py -i "./chapter" -sh 7500 -t .png
```

## Console Usage

```text
python SmartStitchConsole.py [-h] -i INPUT_FOLDER -sh SPLIT_HEIGHT
                             [-t {.png,.jpg,.webp,.bmp,.psd,.tiff,.tga}]
                             [-cw CUSTOM_WIDTH]
                             [-dt {none,pixel}]
                             [-s [0-100]]
                             [-lq [1-100]]
                             [-ip IGNORABLE_PIXELS]
                             [-sl [1-100]]
```

## Build Your Own GUI Package

```bash
python -m scripts.build
```

Build output:
- `dist/SmartStitch/SmartStitch.exe`

## Automatic Update Flow
The app checks latest release from:
- `https://api.github.com/repos/ViminioSM/MedStitch/releases/latest`

Update behavior:
- Compares local app version with release tag.
- If newer tag exists, compiled app can download/extract ZIP update and restart.

Important:
- Release tags must use `v*` format (example: `v3.2.0`).
- Release must include a `.zip` asset.

## GitHub Actions Release Pipeline
Workflow file: `.github/workflows/build.yml`
Auto-tag workflow: `.github/workflows/auto-tag.yml`

Triggers:
- Push to `dev` or `main` (build artifact only).
- Push tag `v*` (build + publish release).
- Push to `dev` or `main` with version in commit title (auto-tag creates `vX.Y.Z`).

Release output asset name:
- `MedStitch-vX.Y.Z-windows.zip`

## Deploying a New Auto-Update Release
1. Commit and push your changes.
2. Put version in the commit title (example: `3.2.0` or `release v3.2.0`).
3. Push to `main` or `dev`.
4. Auto-tag workflow creates `v3.2.0` if it does not exist.
5. Build workflow publishes the GitHub Release automatically.

```bash
git commit -m "3.2.0"
git push origin main
```

6. Confirm GitHub Release was created with ZIP asset.
7. In app, click update check (or restart if auto-check enabled).

## Project Structure (high level)
- `gui/`: UI, controller, process orchestration.
- `console/`: CLI launcher and process flow.
- `core/detectors/`: slicing detectors.
- `core/services/`: image IO, manipulation, settings, watermark, post-process.
- `core/models/`: settings and work directory models.
- `scripts/`: build and helper scripts.

## Troubleshooting
- If context menu entries are duplicated:
  - Remove context menu from app.
  - Install context menu again.
- If update check fails:
  - Verify internet access and GitHub availability.
  - Confirm latest release has valid tag and `.zip` asset.
- If post-process fails:
  - Ensure executable path is valid or command exists in PATH.

## Reporting Issues
Open an issue with:
- What you tried.
- Expected vs actual behavior.
- Logs from `__logs__` folder.
- Sample command/settings used.

## License
This project is licensed under the terms of the LICENSE file in this repository.
