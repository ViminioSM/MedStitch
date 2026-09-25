use numpy::PyArray1;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyTuple};
use rayon::prelude::*;

const SAFETY_BAND_PX: usize = 30;
const MIN_SCAN_STEP: i32 = 30;

fn sensitivity_thresholds(sensitivity: i32) -> (f64, i32, i32, f64) {
    let s = (sensitivity.max(0).min(100) as f64) / 100.0;
    let ratio_thresh = 0.85 + 0.13 * s;
    let black_max = (60.0 - 30.0 * s).round() as i32;
    let white_min = (180.0 + 45.0 * s).round() as i32;
    let std_max = 25.0 - 17.0 * s;
    (ratio_thresh, black_max, white_min, std_max)
}

/// Build boolean mask from raw image bytes.
///
/// Identical decision to the original scalar version, but:
/// - rows are evaluated in parallel with rayon (deterministic order via
///   indexed collect, so `slice_points` never change);
/// - per-row accumulators are integers (`u64`), variance is compared squared
///   (`var < std_max^2`) instead of `sqrt()` per row — same predicate since
///   `std_max >= 0`, no `sqrt` hot loop;
/// - early-out: `std`/variance is only computed when a black/white ratio
///   already passed, gutters fail fast on large art pages.
/// When channels=1, treats data as grayscale (stride = width).
/// When channels>=3, computes ITU-R BT.601 luma from RGB(A) bytes (stride = width * channels).
fn build_perfect_row_mask(
    raw: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    ignorable_pixels: i32,
    sensitivity: i32,
) -> Vec<bool> {
    let left = (ignorable_pixels.max(0) as usize).min(width);
    let right = width.saturating_sub(left);
    if right <= left {
        return vec![false; height];
    }

    let (ratio_thresh, black_max, white_min, std_max) = sensitivity_thresholds(sensitivity);
    let black_max_i = black_max as i32;
    let white_min_i = white_min as i32;
    let active_w = (right - left) as u64;
    let active_w_f = active_w as f64;
    let inv_w = 1.0 / active_w_f;
    let var_max = std_max * std_max;
    let stride = width * channels.max(1);

    (0..height)
        .into_par_iter()
        .map(|y| {
            let row_start = y * stride;
            let mut black_count: u64 = 0;
            let mut white_count: u64 = 0;
            let mut sum: u64 = 0;
            let mut sum_sq: u64 = 0;

            if channels >= 3 {
                for x in left..right {
                    let px = row_start + x * channels;
                    // SAFETY: callers guarantee raw.len() >= width*height*channels.
                    let r = unsafe { *raw.get_unchecked(px) } as u32;
                    let g = unsafe { *raw.get_unchecked(px + 1) } as u32;
                    let b = unsafe { *raw.get_unchecked(px + 2) } as u32;
                    // ITU-R BT.601 luma: integer approximation (matching PIL)
                    let luma = ((r * 299 + g * 587 + b * 114 + 500) / 1000) as u32;
                    sum += luma as u64;
                    sum_sq += (luma as u64) * (luma as u64);
                    if (luma as i32) <= black_max_i {
                        black_count += 1;
                    }
                    if (luma as i32) >= white_min_i {
                        white_count += 1;
                    }
                }
            } else {
                let row = &raw[row_start + left..row_start + right];
                for &pixel in row {
                    let p = pixel as u64;
                    sum += p;
                    sum_sq += p * p;
                    if (pixel as i32) <= black_max_i {
                        black_count += 1;
                    }
                    if (pixel as i32) >= white_min_i {
                        white_count += 1;
                    }
                }
            }

            let black_ratio = black_count as f64 * inv_w;
            let white_ratio = white_count as f64 * inv_w;
            if black_ratio < ratio_thresh && white_ratio < ratio_thresh {
                return false;
            }
            // Same predicate as the original `sqrt(var) < std_max`:
            // original maps tiny-negative fp variance to std=0 (pass).
            let mean = sum as f64 * inv_w;
            let variance = (sum_sq as f64 * inv_w) - (mean * mean);
            if variance > 0.0 {
                variance < var_max
            } else {
                true
            }
        })
        .collect()
}

/// Find centers of all contiguous safety-band-height perfect regions.
fn collect_band_centers(perfect_rows: &[bool], last_row: usize) -> Vec<i32> {
    let band = SAFETY_BAND_PX;
    if last_row < band {
        return Vec::new();
    }

    let mut centers = Vec::new();
    let mut run_len: usize = 0;

    for y in 0..last_row {
        if perfect_rows[y] {
            run_len += 1;
        } else {
            run_len = 0;
        }
        if run_len >= band {
            centers.push((y + 1).saturating_sub(band / 2) as i32);
        }
    }

    centers
}

/// Nearest band center to `row` that is strictly after `last_slice`.
fn nearest_band_center(centers: &[i32], row: i32, last_slice: i32) -> Option<i32> {
    if centers.is_empty() {
        return None;
    }

    let band = SAFETY_BAND_PX as i32;
    let lo = (last_slice + 1).max(row - (band - 1));
    let hi = row + (band - 1);

    let idx = centers.binary_search(&row).unwrap_or_else(|e| e);

    let mut best_center = None;
    let mut best_distance = i32::MAX;

    if idx < centers.len() {
        let c = centers[idx];
        if c > last_slice && c >= lo && c <= hi {
            let d = (c - row).abs();
            if d < best_distance {
                best_distance = d;
                best_center = Some(c);
            }
        }
    }
    if idx > 0 {
        let c = centers[idx - 1];
        if c > last_slice && c >= lo && c <= hi {
            let d = (c - row).abs();
            if d < best_distance {
                best_distance = d;
                best_center = Some(c);
            }
        }
    }
    if idx + 1 < centers.len() {
        let c = centers[idx + 1];
        if c > last_slice && c >= lo && c <= hi {
            let d = (c - row).abs();
            if d < best_distance {
                best_center = Some(c);
            }
        }
    }

    best_center
}

/// Luma of a single pixel (grayscale passthrough or BT.601 for RGB/A).
#[inline]
fn luma_at(raw: &[u8], width: usize, channels: usize, x: usize, y: usize) -> u32 {
    if channels >= 3 {
        let px = y * width * channels + x * channels;
        let r = unsafe { *raw.get_unchecked(px) } as u32;
        let g = unsafe { *raw.get_unchecked(px + 1) } as u32;
        let b = unsafe { *raw.get_unchecked(px + 2) } as u32;
        (r * 299 + g * 587 + b * 114 + 500) / 1000
    } else {
        (unsafe { *raw.get_unchecked(y * width + x) }) as u32
    }
}

/// Stage-2 smart verification for a candidate band center.
///
/// Only vetoes when ink structure is *inside* the 30px band, so clean
/// gutters never fail: vertical ink strokes (drawing contours, balloon
/// borders/tails, text stems), high-variance blocks (screentone, shading)
/// or transition-dense rows (text, hatching, curved borders).
/// Pure-white balloon interiors with no ink in the band still pass here —
/// they lose later to nearby gutters via `gutter_bonus` (panel frame).
fn verify_band(
    raw: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    center: i32,
    black_max: i32,
    std_max: f64,
    left: usize,
    right: usize,
) -> bool {
    let band = SAFETY_BAND_PX as i32;
    let half = band / 2;
    let mut y0 = (center - half).max(0) as usize;
    let y1 = (y0 as i32 + band).min(height as i32) as usize;
    if y1 <= y0 {
        return false;
    }
    if y1 - y0 < band as usize {
        y0 = y1.saturating_sub(band as usize);
    }
    let band_h = (y1 - y0) as u64;
    if band_h == 0 {
        return false;
    }
    let active_w = (right - left) as u64;
    if active_w == 0 {
        return false;
    }

    // Veto V: continuous vertical dark strokes crossing the band.
    let need_dark = (band_h * 4 / 5).max(1);
    let mut bad_cols: u64 = 0;
    let bad_limit = (3u64).max(active_w / 500);
    for x in left..right {
        let mut dark_n: u64 = 0;
        for y in y0..y1 {
            if luma_at(raw, width, channels, x, y) as i32 <= black_max {
                dark_n += 1;
                if dark_n >= need_dark {
                    break;
                }
            }
        }
        if dark_n >= need_dark {
            bad_cols += 1;
            if bad_cols >= bad_limit {
                return false;
            }
        }
    }

    // Veto B: high-variance 64px blocks inside the band (screentone/shading).
    let var_block_max = (std_max * 1.5) * (std_max * 1.5);
    let mut bx = left;
    while bx < right {
        let ex = (bx + 64).min(right);
        let mut sum: u64 = 0;
        let mut sum_sq: u64 = 0;
        let mut n: u64 = 0;
        for y in y0..y1 {
            for x in bx..ex {
                let l = luma_at(raw, width, channels, x, y) as u64;
                sum += l;
                sum_sq += l * l;
                n += 1;
            }
        }
        if n > 0 {
            let mean = sum as f64 / n as f64;
            let var = (sum_sq as f64 / n as f64) - (mean * mean);
            if var >= var_block_max {
                return false;
            }
        }
        bx = ex;
    }

    // Veto T: transition-dense rows (text, hatching, curved borders).
    let trans_limit = (6u64).max(active_w / 100);
    let mid = ((y0 + y1) / 2).min(height - 1);
    let sample_rows = [y0, mid, y1 - 1];
    let mut trans_sum: u64 = 0;
    for &y in &sample_rows {
        let mut trans: u64 = 0;
        let mut prev = luma_at(raw, width, channels, left, y) as i32;
        for x in (left + 1)..right {
            let cur = luma_at(raw, width, channels, x, y) as i32;
            if (cur - prev).abs() > 40 {
                trans += 1;
                if trans > trans_limit * 2 {
                    break;
                }
            }
            prev = cur;
        }
        trans_sum += trans;
    }
    if trans_sum / (sample_rows.len() as u64) > trans_limit {
        return false;
    }

    true
}

/// Bonus for gutter-like candidates: a dark horizontal panel frame within
/// ±5px of the band edge. Balloon interiors have no frame, so nearby gutters
/// win the tie-break without ever vetoing clean bands.
fn gutter_bonus(
    raw: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    center: i32,
    black_max: i32,
    left: usize,
    right: usize,
) -> i32 {
    let band = SAFETY_BAND_PX as i32;
    let half = band / 2;
    let y0 = (center - half).max(0) as usize;
    let y1 = ((center + half).min(height as i32 - 1)).max(0) as usize;
    let active_w = (right - left) as f64;
    if active_w <= 0.0 {
        return 0;
    }
    for dy in -5..=5 {
        for &ye in &[y0 as i32 + dy, y1 as i32 + dy] {
            if ye < 0 || ye >= height as i32 {
                continue;
            }
            let y = ye as usize;
            let mut dark: u64 = 0;
            for x in left..right {
                if luma_at(raw, width, channels, x, y) as i32 <= black_max {
                    dark += 1;
                }
            }
            if dark as f64 / active_w > 0.3 {
                return 300;
            }
        }
    }
    0
}

fn slice_locations(
    raw: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    split_height: i32,
    sensitivity: i32,
    ignorable_pixels: i32,
    scan_step: i32,
    verify_smartcut: bool,
    min_slice: i32,
) -> Vec<i32> {
    if height == 0 {
        return vec![0, 1];
    }

    let split_height = split_height.max(1).max(MIN_SCAN_STEP);
    let min_slice = min_slice.max(0);
    let perfect_rows = build_perfect_row_mask(raw, width, height, channels, ignorable_pixels, sensitivity);
    let band_centers = collect_band_centers(&perfect_rows, height);
    let (_ratio, black_max, _white_min, std_max) = sensitivity_thresholds(sensitivity);
    let left = (ignorable_pixels.max(0) as usize).min(width);
    let right = width.saturating_sub(left);

    // Verified best-of-window: among the ≤3 candidates near `row`, drop
    // anything too short / vetoed by smartcut, prefer panel gutters.
    let pick_center = |row: i32, last_slice: i32| -> Option<i32> {
        let band = SAFETY_BAND_PX as i32;
        let lo = (last_slice + 1).max(row - (band - 1));
        let hi = row + (band - 1);
        let idx = band_centers.binary_search(&row).unwrap_or_else(|e| e);
        let mut best: Option<i32> = None;
        let mut best_score = i32::MAX;
        for di in [0isize, -1, 1] {
            let ci = idx as isize + di;
            if ci < 0 || ci >= band_centers.len() as isize {
                continue;
            }
            let c = band_centers[ci as usize];
            if c <= last_slice || c < lo || c > hi {
                continue;
            }
            if min_slice > 0 && c - last_slice < min_slice {
                continue;
            }
            if verify_smartcut && right > left {
                if !verify_band(raw, width, height, channels, c, black_max, std_max, left, right) {
                    continue;
                }
            }
            let mut score = (c - row).abs() + ((c - last_slice - split_height).abs() / 4);
            if verify_smartcut && right > left {
                score -= gutter_bonus(raw, width, height, channels, c, black_max, left, right);
            }
            if score < best_score {
                best_score = score;
                best = Some(c);
            }
        }
        best
    };

    let mut locations: Vec<i32> = vec![0];
    let mut row: i32 = split_height;
    let mut move_up = true;
    let last_row_i32 = height as i32;

    while row < last_row_i32 {
        let last_slice = *locations.last().unwrap();
        if let Some(bc) = pick_center(row, last_slice) {
            locations.push(bc);
            row = bc + split_height;
            move_up = true;
            continue;
        }
        // If every nearby candidate was vetoed (ink) or too short, keep
        // scanning instead of cutting through art: fall through to move_up/down.
        if nearest_band_center(&band_centers, row, last_slice).is_some() {
            // A raw band exists here but smartcut vetoed it — force scan down
            // past the artwork instead of stepping up into it.
            row += scan_step;
            move_up = false;
            continue;
        }

        if row - last_slice <= (split_height as f64 * 0.4) as i32 {
            row = last_slice + split_height;
            move_up = false;
        } else if move_up {
            row -= scan_step;
            if row <= last_slice {
                row = last_slice + split_height;
                move_up = false;
            }
        } else {
            row += scan_step;
        }
    }

    let remaining = last_row_i32 - locations.last().unwrap();
    if remaining > 50 {
        locations.push(last_row_i32);
    } else if *locations.last().unwrap() != last_row_i32 {
        *locations.last_mut().unwrap() = last_row_i32;
    }

    if locations.first() != Some(&0) {
        locations.insert(0, 0);
    }
    let end_row = last_row_i32.max(1);
    if locations.last() != Some(&end_row) {
        locations.push(end_row);
    }

    let mut normalized: Vec<i32> = Vec::new();
    for &p in &locations {
        let p = p.max(0).min(end_row);
        if normalized.last().map_or(true, |&last| p > last) {
            normalized.push(p);
        }
    }

    if normalized.len() < 2 {
        normalized = vec![0, end_row];
    }

    normalized
}

/// Detect slice points from raw image bytes.
///
/// Args:
///     raw: Raw image bytes (row-major)
///     width: Image width in pixels
///     height: Image height in pixels  
///     split_height: Target height for each slice
///     channels: 1=grayscale, 3=RGB, 4=RGBA (default 1)
#[pyfunction]
#[pyo3(signature = (raw, width, height, split_height, channels=1, sensitivity=90, ignorable_pixels=0, scan_step=30, verify_smartcut=true, min_slice=800))]
fn detect_bytes(
    py: Python<'_>,
    raw: &[u8],
    width: usize,
    height: usize,
    split_height: i32,
    channels: usize,
    sensitivity: i32,
    ignorable_pixels: i32,
    scan_step: i32,
    verify_smartcut: bool,
    min_slice: i32,
) -> Py<PyArray1<i32>> {
    let ch = channels.max(1);
    let expected = width.checked_mul(height).and_then(|v| v.checked_mul(ch)).unwrap_or(0);
    if raw.len() < expected {
        return PyArray1::from_vec(py, vec![0i32, 1]).unbind();
    }

    let result = slice_locations(
        raw,
        width,
        height,
        ch,
        split_height,
        sensitivity.max(0).min(100),
        ignorable_pixels.max(0),
        scan_step.max(MIN_SCAN_STEP),
        verify_smartcut,
        min_slice,
    );

    PyArray1::from_vec(py, result).unbind()
}

/// Unified pipeline: combine → gray → detect → slice in one Rust call.
///
/// Args:
///     images: list of (raw_bytes, width, height, mode) - mode is "RGB" or "RGBA"
///     split_height: target slice height
#[pyfunction]
#[pyo3(signature = (images, split_height, sensitivity=90, ignorable_pixels=0, scan_step=30))]
fn stitch_pipeline(
    py: Python<'_>,
    images: &Bound<'_, PyList>,
    split_height: i32,
    sensitivity: i32,
    ignorable_pixels: i32,
    scan_step: i32,
) -> PyResult<Py<PyList>> {
    let sensitivity = sensitivity.max(0).min(100);
    let ignorable_pixels = ignorable_pixels.max(0);
    let scan_step = scan_step.max(MIN_SCAN_STEP);
    let split_height = split_height.max(1).max(MIN_SCAN_STEP);
    let n = images.len();
    if n == 0 {
        return Ok(PyList::empty(py).unbind());
    }

    // --- Parse input images, compute max width and total height ---
    struct ImageInfo {
        #[allow(dead_code)]
        idx: usize,
        width: usize,
        height: usize,
        channels: usize, // 3 for RGB, 4 for RGBA
        raw: Vec<u8>,     // owned copy
    }

    let mut infos: Vec<ImageInfo> = Vec::with_capacity(n);
    let mut max_width: usize = 0;
    let mut total_height: usize = 0;
    let mut output_channels: usize = 3; // will be 4 only if ALL inputs are RGBA

    // First pass: check if all are RGBA
    for i in 0..n {
        let tup: &Bound<'_, PyTuple> = &images.get_item(i)?.downcast_into()?;
        let mode: String = tup.get_item(3)?.extract()?;
        if mode != "RGBA" {
            output_channels = 3;
            break;
        }
        if i == n - 1 {
            output_channels = 4;
        }
    }

    // Second pass: collect metadata
    for i in 0..n {
        let tup: &Bound<'_, PyTuple> = &images.get_item(i)?.downcast_into()?;
        let raw_py: &Bound<'_, PyBytes> = &tup.get_item(0)?.downcast_into()?;
        let width: usize = tup.get_item(1)?.extract()?;
        let height: usize = tup.get_item(2)?.extract()?;
        let mode: String = tup.get_item(3)?.extract()?;

        let channels = if mode == "RGBA" { 4 } else { 3 };
        let expected = width * height * channels;
        let raw_bytes = raw_py.as_bytes();
        let owned = if raw_bytes.len() >= expected {
            raw_bytes[..expected].to_vec()
        } else {
            let mut v = vec![0u8; expected];
            let copy = raw_bytes.len().min(expected);
            v[..copy].copy_from_slice(&raw_bytes[..copy]);
            v
        };

        max_width = max_width.max(width);
        total_height += height;
        infos.push(ImageInfo { idx: i, width, height, channels, raw: owned });
    }

    if max_width == 0 || total_height == 0 {
        return Ok(PyList::empty(py).unbind());
    }

    let stride = max_width * output_channels;
    let combined_size = total_height * stride;
    let mut combined: Vec<u8> = vec![0u8; combined_size];
    let mut gray: Vec<u8> = vec![0u8; total_height * max_width];

    // --- Vstack + grayscale in one pass ---
    let mut y_offset: usize = 0;
    for info in &infos {
        let src_channels = info.channels;
        let src_stride = info.width * src_channels;
        let pad = (max_width - info.width) * output_channels;

        for row in 0..info.height {
            let src_start = row * src_stride;
            let dst_start = (y_offset + row) * stride;
            let gray_start = (y_offset + row) * max_width;

            // Copy RGB/RGBA row
            if src_channels == output_channels {
                combined[dst_start..dst_start + src_stride]
                    .copy_from_slice(&info.raw[src_start..src_start + src_stride]);
            } else if output_channels == 3 && src_channels == 4 {
                // RGBA → RGB: drop alpha
                for x in 0..info.width {
                    combined[dst_start + x * 3] = info.raw[src_start + x * 4];
                    combined[dst_start + x * 3 + 1] = info.raw[src_start + x * 4 + 1];
                    combined[dst_start + x * 3 + 2] = info.raw[src_start + x * 4 + 2];
                }
                for x in 0..pad {
                    combined[dst_start + info.width * 3 + x] = 0;
                }
            } else {
                // RGB → RGBA: add alpha=255
                for x in 0..info.width {
                    combined[dst_start + x * 4] = info.raw[src_start + x * 3];
                    combined[dst_start + x * 4 + 1] = info.raw[src_start + x * 3 + 1];
                    combined[dst_start + x * 4 + 2] = info.raw[src_start + x * 3 + 2];
                    combined[dst_start + x * 4 + 3] = 255;
                }
                for x in 0..pad {
                    combined[dst_start + info.width * 4 + x] = 0;
                }
            }

            // Compute grayscale (ITU-R BT.601 luma) from the source row
            if src_channels >= 3 {
                let mut gray_acc: f64 = 0.0;
                let active_pixels = info.width;
                for x in 0..active_pixels {
                    let r = info.raw[src_start + x * src_channels] as f64;
                    let g = info.raw[src_start + x * src_channels + 1] as f64;
                    let b = info.raw[src_start + x * src_channels + 2] as f64;
                    // ITU-R BT.601
                    let luma = 0.299 * r + 0.587 * g + 0.114 * b;
                    gray[gray_start + x] = luma.round().clamp(0.0, 255.0) as u8;
                    gray_acc += luma;
                }
                // Pad grayscale with the average luma of the row (or 0)
                let avg = if active_pixels > 0 {
                    (gray_acc / active_pixels as f64).round().clamp(0.0, 255.0) as u8
                } else {
                    0u8
                };
                for x in active_pixels..max_width {
                    gray[gray_start + x] = avg;
                }
            } else {
                // Grayscale input (shouldn't happen, but handle)
                for x in 0..info.width {
                    gray[gray_start + x] = info.raw[src_start + x];
                }
                for x in info.width..max_width {
                    gray[gray_start + x] = 128;
                }
            }
        }
        y_offset += info.height;
    }

    // --- Detect slice points ---
    let locations = slice_locations(
        &gray,
        max_width,
        total_height,
        1, // already grayscale
        split_height,
        sensitivity,
        ignorable_pixels,
        scan_step,
        true,
        800,
    );

    // --- Extract slices from combined buffer ---
    let result = PyList::empty(py);
    let end_row = total_height as i32;
    let mode_str = if output_channels == 4 { "RGBA" } else { "RGB" };

    for w in locations.windows(2) {
        let y1 = w[0].max(0) as usize;
        let y2 = (w[1].min(end_row) as usize).max(y1);
        if y2 <= y1 {
            continue;
        }
        let slice_h = y2 - y1;
        let slice_bytes = combined[y1 * stride..y2 * stride].to_vec();
        let py_bytes = PyBytes::new(py, &slice_bytes);

        let tup = PyTuple::new(
            py,
            &[
                py_bytes.into_any(),
                (max_width as i64).into_pyobject(py)?.into_any(),
                (slice_h as i64).into_pyobject(py)?.into_any(),
                mode_str.into_pyobject(py)?.into_any(),
            ],
        )?;
        result.append(tup)?;
    }

    Ok(result.unbind())
}
#[pyfunction]
#[pyo3(signature = (gray, split_height, sensitivity=None, ignorable_pixels=None, scan_step=None, verify_smartcut=None, min_slice=None))]
fn detect(
    py: Python<'_>,
    gray: numpy::PyReadonlyArray2<u8>,
    split_height: i32,
    sensitivity: Option<i32>,
    ignorable_pixels: Option<i32>,
    scan_step: Option<i32>,
    verify_smartcut: Option<bool>,
    min_slice: Option<i32>,
) -> Py<PyArray1<i32>> {
    let array = gray.as_array();
    let (h, w) = array.dim();
    if h == 0 {
        return PyArray1::from_vec(py, vec![0i32, 1]).unbind();
    }
    let verify = verify_smartcut.unwrap_or(true);
    let min_s = min_slice.unwrap_or(800);

    // Zero-copy when the ndarray is C-contiguous: borrow the memory directly
    // instead of `to_vec()` (saves 1x W*H copy on huge chapters).
    if let Some(slice) = array.as_slice_memory_order() {
        let result = slice_locations(
            slice,
            w,
            h,
            1, // already grayscale from numpy
            split_height,
            sensitivity.unwrap_or(90).max(0).min(100),
            ignorable_pixels.unwrap_or(0).max(0),
            scan_step.unwrap_or(30).max(MIN_SCAN_STEP),
            verify,
            min_s,
        );
        return PyArray1::from_vec(py, result).unbind();
    }

    let raw: Vec<u8> = array.iter().copied().collect();

    let result = slice_locations(
        &raw,
        w,
        h,
        1, // already grayscale from numpy
        split_height,
        sensitivity.unwrap_or(90).max(0).min(100),
        ignorable_pixels.unwrap_or(0).max(0),
        scan_step.unwrap_or(30).max(MIN_SCAN_STEP),
        verify,
        min_s,
    );

    PyArray1::from_vec(py, result).unbind()
}

#[pymodule]
fn smartstitch_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(detect_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(detect, m)?)?;
    m.add_function(wrap_pyfunction!(stitch_pipeline, m)?)?;
    Ok(())
}
