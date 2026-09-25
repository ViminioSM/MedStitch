# SmartStitch Console (CLI) — Reference for Agents / Bots

**Audience:** another AI, automation script, or bot that must call SmartStitch headless.  
**Product names:** SmartStitch / MedStitch (same codebase).  
**Entry point:** `SmartStitchConsole.py` (or `python -m console.launcher` from repo root).

This document is the source of truth for CLI usage. Prefer **JSON config** for complex jobs; use flags for simple overrides.

---

## 1. What it does

Vertical comic / manhwa pipeline (same engine as the GUI):

1. Discover folders with images under `-i` / `input_folder`
2. Load images (optional PSD first-layer only)
3. Optional width enforce (resize Lanczos)
4. Combine vertically → detect slice points → slice
5. Optional compact top white (>400px)
6. Save pages (`output_type`)
7. Optional watermarks (fullpage / overlay / header / footer)
8. Optional external postprocess (e.g. Waifu2X) via `[stitched]` / `[processed]` tokens
9. Optional ComicZip of stitched output

**Engine:** `core.services.stitch_process.StitchProcess` (identical to GUI `GuiStitchProcess`).  
CLI only builds settings + paths, then calls that engine.

---

## 2. How to invoke

### From source (repo root)

```bash
python SmartStitchConsole.py [options]
```

Windows PowerShell:

```powershell
python .\SmartStitchConsole.py -i "D:\caps\ch01" -sh 5000 -t .png
```

### Requirements

- Python 3.11+ recommended
- Dependencies: `requirements.txt` / Pipfile (Pillow, numpy, natsort, psd-tools, etc.)
- Run **from project root** so `core` / `console` packages resolve
- Working directory should be the repo root (or PYTHONPATH including it)

### Help

```bash
python SmartStitchConsole.py -h
```

---

## 3. Exit codes (critical for bots)

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Runtime error (missing folder, processing failure, postprocess fail, etc.). Message on **stderr** as `ERROR: ...` |
| `2` | Argparse error (missing required args, invalid flag). Message on stderr |

Always check exit code. With `-q` / `--quiet`, progress is suppressed but exit codes remain valid.

---

## 4. Settings resolution order

1. **Base**
   - Default: **console defaults** (see §5) — does **not** load GUI profile
   - With `--use-saved-settings`: load GUI saved profile from `%APPDATA%\SmartStitch\__settings__\settings.json` (or equivalent)
2. **JSON** (`-c` / `--config`): merged as base; then CLI overrides
3. **CLI flags**: non-`None` values win over JSON
4. **`--waifu` preset**: fills postprocess fields if not already set
5. **Waifu auto-install**: if `--waifu` and default exe missing → download (same URL as GUI)

Merge rule for config + CLI: config first, then every CLI key with value `!= None` overwrites.

---

## 5. Console defaults (when NOT using `--use-saved-settings`)

Applied with `setdefault` only if the user did not set them:

| Key | Default |
|-----|---------|
| `output_type` | `.png` |
| `sensitivity` | `90` |
| `ignorable_pixels` | `5` |
| `scan_step` | `30` |
| `enforce_type` | `0` (NONE — **no resize**) |
| `parallel_processing` | `true` |
| `lossy_quality` | `100` |
| `detector_type` | `1` (pixel comparison) |
| `split_height` | `5000` |

**Important:** GUI defaults often force width 800 + JPG. Console defaults intentionally do **not** resize unless you pass `-cw` / `--enforce-*`.

To match GUI profile exactly:

```bash
python SmartStitchConsole.py -i "D:\caps" --use-saved-settings
```

---

## 6. Paths and folder layout

### Input (`-i` / `input_folder`) — **required**

- Must be an existing directory
- Recursively finds subfolders that contain supported images
- Each such folder is one “work directory” (chapter)

**Supported input extensions:**  
`.png`, `.svg`, `.avif`, `.webp`, `.jpg`, `.jpeg`, `.jfif`, `.bmp`, `.tiff`, `.tga`, `.psd`, `.psb`

### Output (`-o` / `output_path`)

- Optional
- Default: `<input_path> [stitched]` (suffix with a space before `[`)
- Relative structure under input is mirrored under output

### Postprocess / ComicZip path (`--postprocess-path`)

- Optional
- Default: `<input_path> [processed]`
- Used by:
  - External postprocess output token `[processed]`
  - ComicZip archive destination (when `--comiczip`)

### Example tree

```
D:\caps\ch01\          ← -i
  01.png
  02.png
D:\caps\ch01 [stitched]\   ← default -o
  01.png
  02.jpg
D:\caps\ch01 [processed]\  ← default --postprocess-path / ComicZip here
  ch01.zip                 ← if --comiczip
```

---

## 7. Pipeline steps (order)

Exact order inside the engine:

```
explore dirs
  → load
  → resize (if enforce ≠ none)
  → combine
  → detect slices
  → slice
  → compact top white (if enabled)
  → save
  → watermarks (if any enabled + paths exist)
  → external postprocess (if run_postprocess)
  → comiczip (if run_comiczip)
```

Parallel mode: multiple chapter folders at once (max 5 concurrent), each still sequential inside.

---

## 8. CLI reference

### Paths

| Flag | Dest | Notes |
|------|------|--------|
| `-i`, `--input` | `input_folder` | Required unless in JSON |
| `-o`, `--output` | `output_path` | Optional |
| `--postprocess-path` | `postprocess_path` | Optional |
| `-c`, `--config` | `config` | JSON file path |

### Stitch

| Flag | Dest | Values / range |
|------|------|----------------|
| `-sh`, `--split-height` | `split_height` | positive int (px) |
| `-t`, `--output-type` | `output_type` | `.png` `.avif` `.jpg` `.jpeg` `.webp` `.bmp` `.tiff` `.tga` `.psd` |
| `-lq`, `--lossy-quality` | `lossy_quality` | 1–100 (jpg/webp); AVIF always lossless |
| `-dt`, `--detection-type` | `detection_type` | `none` \| `pixel` |
| `-s`, `--sensitivity` | `detection_sensitivity` | 0–100 (higher = stricter cuts) |
| `-ip`, `--ignorable-pixels` | `ignorable_pixels` | ≥ 0 |
| `-sl`, `--scan-step` | `scan_line_step` | 1–99 (engine enforces min ~30 for pixel detector) |
| `--psd-first-layer` | `psd_first_layer_only` | flag |

### Width

| Flag | Dest | Notes |
|------|------|--------|
| `-cw`, `--custom-width` | `custom_width` | >0 ⇒ enforce manual + that width; ≤0 disables |
| `--enforce-type` | `enforce_type` | `none` \| `auto` \| `manual` |
| `--enforce-width` | `enforce_width` | used with manual |

### Pipeline toggles

Boolean flags use `--foo` / `--no-foo` pattern (`BooleanOptionalAction`).

| Flag | Dest |
|------|------|
| `--parallel` / `--no-parallel` | `parallel_processing` |
| `--comiczip` / `--no-comiczip` | `run_comiczip` |
| `--compact` / `--no-compact` | `postprocess_compact_enabled` |
| `--postprocess` / `--no-postprocess` | `run_postprocess` |
| `--postprocess-app PATH` | `postprocess_app` |
| `--postprocess-args "..."` | `postprocess_args` |
| `--waifu [auto\|jpg\|webp]` | `waifu` |
| `--waifu-repair` | `waifu_repair` |

### Watermark fullpage

| Flag | Dest |
|------|------|
| `--wm-fullpage` / `--no-wm-fullpage` | enabled |
| `--wm-fullpage-paths PATH [PATH ...]` | paths (semicolon-joined internally) |
| `--wm-fullpage-position` | `top` \| `center` \| `bottom` |
| `--wm-fullpage-frequency` | `once` \| `all` \| `alternating` |
| `--wm-fullpage-max N` | max per page |
| `--wm-fullpage-strategy` | `first` \| `best` \| `random` |
| `--wm-fullpage-insert` / `--no-wm-fullpage-insert` | insert vs overlay paste |
| `--wm-fullpage-min-area N` | min block height |
| `--wm-fullpage-alt-interval N` | alternating interval |

If paths are given without enabling flag, **enabled is implied**.

### Watermark overlay

| Flag | Dest |
|------|------|
| `--wm-overlay` / `--no-wm-overlay` | enabled |
| `--wm-overlay-paths PATH [PATH ...]` | paths |
| `--wm-overlay-position` | `auto` `top_left` `top_right` `bottom_left` `bottom_right` `center` |
| `--wm-overlay-opacity` | 0–100 |
| `--wm-overlay-scale` | 5–100 (% of page width) |
| `--wm-overlay-max N` | max per page |
| `--wm-overlay-margin N` | px |

### Header / footer

| Flag | Notes |
|------|--------|
| `--header` / `--no-header` | first page of chapter only |
| `--header-paths PATH ...` | implies enable if paths set |
| `--footer` / `--no-footer` | last page of chapter only |
| `--footer-paths PATH ...` | implies enable if paths set |

### Runtime

| Flag | Notes |
|------|--------|
| `--use-saved-settings` / `--no-use-saved-settings` | default: **false** |
| `-q`, `--quiet` | no progress on stdout |

---

## 9. JSON config (`-c` / `--config`)

### Rules

- Root must be a JSON **object**
- CLI flags override JSON keys when the CLI value is not `null`/omitted
- Path keys accepted: `input_folder`, `input_path`, `input`
- Output keys: `output_path`, `output`
- Postprocess dir: `postprocess_path`, `processed_path`
- Paths for watermarks: string with `;` separators **or** JSON array of strings
- Booleans: JSON `true`/`false`
- `waifu`: `true` \| `false` \| `"auto"` \| `"jpg"` \| `"webp"`

### Full example (bot-oriented)

```json
{
  "input_folder": "D:/work/chapter_01",
  "output_path": "D:/work/out_stitched",
  "postprocess_path": "D:/work/out_processed",

  "split_height": 15000,
  "output_type": ".jpg",
  "lossy_quality": 100,
  "detection_type": "pixel",
  "sensitivity": 100,
  "ignorable_pixels": 0,
  "scan_step": 30,

  "enforce_type": "manual",
  "enforce_width": 800,
  "custom_width": 800,

  "parallel_processing": true,
  "postprocess_compact_enabled": false,
  "run_comiczip": true,

  "waifu": "jpg",
  "waifu_repair": false,

  "watermark_fullpage_paths": ["D:/assets/wm_full.png"],
  "watermark_fullpage_position": "center",
  "watermark_fullpage_frequency": "once",
  "watermark_fullpage_max_per_page": 1,
  "watermark_fullpage_block_strategy": "best",
  "watermark_fullpage_insert_mode": true,
  "watermark_fullpage_min_area_height": 400,

  "watermark_overlay_paths": ["D:/assets/wm_ov.png"],
  "watermark_overlay_position": "auto",
  "watermark_overlay_opacity": 80,
  "watermark_overlay_scale_pct": 50,
  "watermark_overlay_max_per_page": 1,
  "watermark_overlay_margin": 10,

  "watermark_header_paths": ["D:/assets/header.png"],
  "watermark_footer_paths": ["D:/assets/footer.png"]
}
```

### Minimal examples

**Stitch only:**

```json
{
  "input_folder": "D:/caps/ch01",
  "split_height": 5000,
  "output_type": ".png"
}
```

**Stitch + Waifu + zip:**

```json
{
  "input_folder": "D:/caps/ch01",
  "split_height": 15000,
  "output_type": ".jpg",
  "custom_width": 800,
  "waifu": true,
  "run_comiczip": true
}
```

**Use GUI profile + override input only (CLI):**

```bash
python SmartStitchConsole.py -i "D:\caps\ch01" --use-saved-settings -q
```

### Snapshot field names (JSON / overrides)

These map 1:1 into the engine snapshot (plus path/runtime keys above):

```
split_height, output_type, lossy_quality,
enforce_type, enforce_width,
detector_type, sensitivity, ignorable_pixels, scan_step,
run_postprocess, postprocess_compact_enabled, run_comiczip, parallel_processing,
postprocess_app, postprocess_args,
watermark_fullpage_enabled, watermark_fullpage_paths,
watermark_fullpage_position, watermark_fullpage_frequency,
watermark_fullpage_threshold, watermark_fullpage_alternate_interval,
watermark_fullpage_max_per_page, watermark_fullpage_block_strategy,
watermark_fullpage_insert_mode, watermark_fullpage_min_area_height,
watermark_fullpage_min_spacing_top, watermark_fullpage_min_spacing_bottom,
watermark_fullpage_min_spacing_sides, watermark_fullpage_require_centered_space,
watermark_overlay_enabled, watermark_overlay_paths,
watermark_overlay_position, watermark_overlay_opacity,
watermark_overlay_scale_pct, watermark_overlay_max_per_page, watermark_overlay_margin,
watermark_header_enabled, watermark_header_paths,
watermark_footer_enabled, watermark_footer_paths
```

Aliases accepted by the console layer:

| Alias | Maps to |
|-------|---------|
| `detection_type` (`none`/`pixel`) | `detector_type` 0/1 |
| `detection_sensitivity` | `sensitivity` |
| `scan_line_step` | `scan_step` |
| `custom_width` | `enforce_type=manual` + `enforce_width` |

Enum strings accepted in JSON/CLI (normalized internally):

- Fullpage position: `top`, `center`, `bottom`
- Frequency: `once`, `all`, `alternating` (also `once_per_page`, `all_blocks`)
- Block strategy: `first`, `best`, `random`
- Overlay position: `auto`, `top_left`, `top_right`, `bottom_left`, `bottom_right`, `center`
- Enforce: `none`, `auto`/`automatic`, `manual`

---

## 10. Postprocess (external app)

### Manual

```bash
python SmartStitchConsole.py -i "D:\caps" -sh 5000 \
  --postprocess \
  --postprocess-app "C:\Manhwa\Waifu2X\waifu2x-ncnn-vulkan.exe" \
  --postprocess-args "-i [stitched] -o [processed] -n 3 -s 1 -f jpg -q 100"
```

### Tokens (required semantics)

| Token | Replaced with |
|-------|----------------|
| `[stitched]` | Chapter stitched output folder (`work_dir.output_path`) |
| `[processed]` | Chapter postprocess folder (`work_dir.postprocess_path`) |

### Rules

- `--postprocess` alone without app → **fails** unless app comes from `--waifu` or saved settings
- App must exist as absolute file path or be on `PATH`
- Same `PostProcessRunner` as GUI
- Failure of external process → exit code `1`

### Compact vs postprocess

| Feature | Flag | When | What |
|---------|------|------|------|
| Compact | `--compact` | After slice, before save | Crops large pure-white top area (>400px) |
| Postprocess | `--postprocess` / `--waifu` | After save (+ watermarks) | External executable |

---

## 11. Waifu2X preset (`--waifu`)

### Behavior

1. Sets `run_postprocess = true`
2. Default app: `C:/Manhwa/Waifu2X/waifu2x-ncnn-vulkan.exe`
3. Default args:
   - **jpg:** `-i [stitched] -o [processed] -n 3 -s 1 -f jpg -q 100`
   - **webp:** `-i [stitched] -o [processed] -n 3 -s 1 -f webp`
4. Format selection:
   - `--waifu` or `--waifu auto` or JSON `"waifu": true` → **auto**: `.webp` output ⇒ webp args, else jpg args
   - `--waifu jpg` / `--waifu webp` forces args format
5. If default exe **missing** → **auto-download** from  
   `https://github.com/ViminioSM/MedStitch/releases/download/waifu2x/Waifu2X.zip`  
   (same as GUI Install button) into `C:/Manhwa/Waifu2X`
6. `--waifu-repair` forces re-download

### Overrides

- `--postprocess-app` / `--postprocess-args` win over preset args/app
- `--no-postprocess` disables even if `--waifu` set
- Custom absolute `--postprocess-app` path: **no** auto-download for that path

### Examples

```bash
# Auto install + jpg args (because -t .jpg)
python SmartStitchConsole.py -i "D:\caps" -sh 15000 -t .jpg -cw 800 --waifu -q

# Force webp upscale args
python SmartStitchConsole.py -i "D:\caps" --waifu webp

# Repair install then run
python SmartStitchConsole.py -i "D:\caps" --waifu --waifu-repair
```

---

## 12. Quality notes (for correct expectations)

| Output | Quality |
|--------|---------|
| `.png` | Lossless encode |
| `.avif` | Lossless mode in this app |
| `.jpg` / `.webp` | Lossy; use `-lq 100` for best |
| Width enforce | Lanczos resize if width changes — not bit-identical to input |
| Watermark on JPG | Second encode pass — extra loss |

Default console path without flags: PNG + no width enforce → high fidelity.

---

## 13. Recommended recipes for bots

### A. Simple stitch (max quality)

```bash
python SmartStitchConsole.py -i "INPUT" -sh 5000 -t .png -q
```

### B. Production manhwa (match common GUI “redraw” style)

```bash
python SmartStitchConsole.py -i "INPUT" -sh 15000 -t .jpg -lq 100 -cw 800 \
  -dt pixel -s 100 -sl 30 -ip 0 --waifu --parallel -q
```

### C. Type preset style (webp + direct slice + waifu webp)

```bash
python SmartStitchConsole.py -i "INPUT" -sh 5000 -t .webp -cw 800 \
  -dt none --waifu webp -q
```

### D. Full automation via JSON

```bash
python SmartStitchConsole.py -c "D:\jobs\job01.json" -q
echo %ERRORLEVEL%
```

### E. Mirror GUI session settings

```bash
python SmartStitchConsole.py -i "INPUT" --use-saved-settings -q
```

---

## 14. Pseudocode for an orchestrating AI

```
1. Ensure cwd = SmartStitch repo root (or set PYTHONPATH)
2. Validate input_folder exists and contains images
3. Build either:
   a) argv list for CLI, or
   b) write job.json then ["python", "SmartStitchConsole.py", "-c", job.json, "-q"]
4. subprocess.run(..., check=False)
5. if returncode != 0: read stderr, fail job
6. if returncode == 0:
     outputs live under:
       - output_path or "{input} [stitched]"
       - postprocess_path or "{input} [processed]" if postprocess/comiczip ran
7. Do not assume GUI must be open
8. Do not edit settings.json unless using --use-saved-settings intentionally
```

### Python invoke example

```python
import subprocess
import sys

cmd = [
    sys.executable,
    "SmartStitchConsole.py",
    "-i", r"D:\caps\ch01",
    "-sh", "15000",
    "-t", ".jpg",
    "-lq", "100",
    "-cw", "800",
    "--waifu",
    "-q",
]
result = subprocess.run(cmd, cwd=r"C:\path\to\SmartStitch", capture_output=True, text=True)
if result.returncode != 0:
    raise RuntimeError(result.stderr or result.stdout)
```

---

## 15. Environment variables (optional)

| Variable | Effect |
|----------|--------|
| `SMARTSTITCH_LOAD_WORKERS` | Parallel image load workers |
| `SMARTSTITCH_SAVE_WORKERS` | Parallel save workers |
| `SMARTSTITCH_FAST_SAVE` | Faster/lower quality JPEG/WebP encode knobs |
| `SMARTSTITCH_WATERMARK_WORKERS` | Watermark thread count |
| `SMARTSTITCH_WM_DEBUG` / `MEDSTITCH_WM_DEBUG` | Verbose watermark logs |
| `SMARTSTITCH_DEBUG_LOG` | File debug logging under app data logs |
| Benchmark enable (if set by `is_benchmark_enabled` env) | Writes benchmark JSON under logs |

Bots usually leave these unset.

---

## 16. What the console is NOT

- Not a GUI launcher
- Does not open Qt windows
- Does not install Windows context menu
- Does not auto-check app updates
- Does not modify GUI settings unless you only *read* them via `--use-saved-settings` (processing does not require saving profile from CLI)

---

## 17. Failure modes to handle

| Symptom | Likely cause |
|---------|----------------|
| exit 2 | Missing `-i` and no config; bad flag |
| exit 1 `Input folder not found` | Bad path |
| exit 1 `No valid work directories` | No supported images under input |
| exit 1 postprocess not found | Waifu missing and download failed, or bad `--postprocess-app` |
| exit 1 postprocess CalledProcessError | Waifu/external app non-zero exit |
| exit 1 dimension / encoder errors | Extremely tall images; engine may retry sensitivity automatically |

---

## 18. Source map (for debugging)

| Piece | Path |
|-------|------|
| CLI argv | `console/launcher.py` |
| Settings merge / waifu / JSON | `console/process.py` |
| Shared pipeline | `core/services/stitch_process.py` |
| External postprocess | `core/services/postprocess_runner.py` |
| Waifu download | `core/services/waifu2x_installer.py` |
| Entry | `SmartStitchConsole.py` |

---

## 19. Quick checklist for another AI

- [ ] Run from repo root with `python SmartStitchConsole.py`
- [ ] Always provide `input_folder` (flag or JSON)
- [ ] Prefer `-q` and check exit code
- [ ] Prefer JSON for multi-option jobs
- [ ] Use `--waifu` for Waifu2X (auto-download default path)
- [ ] Use `-cw` / enforce only when resize is desired
- [ ] Remember CLI defaults ≠ GUI profile unless `--use-saved-settings`
- [ ] Outputs: ` [stitched]` and optional ` [processed]`
- [ ] Same image engine as GUI — only settings source differs

---

*Generated for agent consumption from the SmartStitch console implementation.*
)
