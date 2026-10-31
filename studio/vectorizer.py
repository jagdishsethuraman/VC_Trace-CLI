#!/usr/bin/env python3
"""
Custom Python Image Vectorizer (Raster to SVG)
Author: Google Deepmind Antigravity Pair Programmer
Architecture:
- Native Dual-Engine Loader: Uses PIL (Pillow) if available, with pure-Python PNG/BMP fallback.
- Color Quantization: Adaptive palette clustering (K-Means / Median Cut) & Otsu auto-thresholding.
- Boundary Extraction: Eulerian directed-edge lattice tracing with deterministic saddle-point resolution.
- Topology Handling: Automatic hole/cutout detection via signed Shoelace area and SVG evenodd fill-rule.
- Path Simplification: Collinear reduction + Ramer-Douglas-Peucker (RDP) algorithm.
- Curve Fitting: Catmull-Rom to Cubic Bézier conversion with adaptive sharp-corner preservation.
"""

import sys
import os
import math
import struct
import zlib
import argparse
from collections import defaultdict

# Optional PIL support
try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ==============================================================================
# 1. PURE-PYTHON IMAGE DECODERS (FALLBACK WHEN PIL IS NOT INSTALLED)
# ==============================================================================

def decode_png(source):
    """Decode standard PNG (Grayscale, RGB, RGBA, Palette) using standard library."""
    if isinstance(source, (bytes, bytearray)):
        data = source
    else:
        with open(source, "rb") as f:
            data = f.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG signature")

    idx = 8
    width, height, bit_depth, color_type = 0, 0, 0, 0
    idat = bytearray()
    plte = None

    while idx < len(data):
        length = struct.unpack(">I", data[idx:idx + 4])[0]
        chunk_type = data[idx + 4:idx + 8]
        chunk_data = data[idx + 8:idx + 8 + length]
        idx += 8 + length + 4  # 4 len + 4 type + data + 4 crc

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk_data)
            if bit_depth != 8:
                raise ValueError(f"Only 8-bit PNG supported in pure-Python fallback (got {bit_depth}-bit)")
        elif chunk_type == b"PLTE":
            plte = chunk_data
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    raw = zlib.decompress(bytes(idat))
    bpp_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in bpp_map:
        raise ValueError(f"Unsupported PNG color type: {color_type}")
    bpp = bpp_map[color_type]
    stride = width * bpp

    pixels = []
    prev_row = bytearray(stride)
    raw_idx = 0

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)

    for y in range(height):
        filter_type = raw[raw_idx]
        raw_idx += 1
        curr_row = bytearray(raw[raw_idx:raw_idx + stride])
        raw_idx += stride

        recon = bytearray(stride)
        for x in range(stride):
            left = recon[x - bpp] if x >= bpp else 0
            up = prev_row[x]
            up_left = prev_row[x - bpp] if x >= bpp else 0
            val = curr_row[x]

            if filter_type == 0:
                rc = val
            elif filter_type == 1:
                rc = (val + left) & 0xFF
            elif filter_type == 2:
                rc = (val + up) & 0xFF
            elif filter_type == 3:
                rc = (val + ((left + up) >> 1)) & 0xFF
            elif filter_type == 4:
                rc = (val + paeth(left, up, up_left)) & 0xFF
            else:
                rc = val
            recon[x] = rc

        prev_row = recon
        row_rgb = []
        for i in range(0, stride, bpp):
            if color_type == 2:  # RGB
                row_rgb.append((recon[i], recon[i + 1], recon[i + 2]))
            elif color_type == 6:  # RGBA (alpha blend over white)
                a = recon[i + 3] / 255.0
                r = int(recon[i] * a + 255 * (1 - a))
                g = int(recon[i + 1] * a + 255 * (1 - a))
                b = int(recon[i + 2] * a + 255 * (1 - a))
                row_rgb.append((r, g, b))
            elif color_type == 0:  # Grayscale
                v = recon[i]
                row_rgb.append((v, v, v))
            elif color_type == 3:  # Palette
                p_idx = recon[i] * 3
                row_rgb.append((plte[p_idx], plte[p_idx + 1], plte[p_idx + 2]))
        pixels.append(row_rgb)

    return width, height, pixels


def decode_bmp(source):
    """Decode uncompressed 24-bit / 32-bit BMP."""
    if isinstance(source, (bytes, bytearray)):
        data = source
    else:
        with open(source, "rb") as f:
            data = f.read()

    if data[:2] != b"BM":
        raise ValueError("Invalid BMP signature")

    offset = struct.unpack("<I", data[10:14])[0]
    w, h, _, bpp, compression = struct.unpack("<iiHHI", data[18:34])
    if compression != 0 or bpp not in (24, 32):
        raise ValueError(f"Only uncompressed 24/32-bit BMP supported (got bpp={bpp}, comp={compression})")

    is_top_down = h < 0
    h = abs(h)
    bytes_per_px = bpp // 8
    row_bytes = (w * bytes_per_px + 3) & ~3

    pixels = []
    for y in range(h):
        row_y = y if is_top_down else (h - 1 - y)
        row_start = offset + row_y * row_bytes
        row = []
        for x in range(w):
            px_idx = row_start + x * bytes_per_px
            b, g, r = data[px_idx], data[px_idx + 1], data[px_idx + 2]
            row.append((r, g, b))
        pixels.append(row)

    return w, h, pixels


def load_image(source, max_dimension=None):
    """Load image from filepath or raw bytes returning (width, height, 2D list of (R, G, B) tuples)."""
    if isinstance(source, (bytes, bytearray)):
        if HAS_PIL:
            import io
            img = PILImage.open(io.BytesIO(source)).convert("RGBA")
        else:
            if source[:8] == b"\x89PNG\r\n\x1a\n":
                return decode_png(source)
            elif source[:2] == b"BM":
                return decode_bmp(source)
            else:
                raise RuntimeError("Format requires Pillow (`pip install pillow`). Fallback only supports PNG and BMP.")
    else:
        if HAS_PIL:
            img = PILImage.open(source).convert("RGBA")
        else:
            ext = os.path.splitext(source)[1].lower()
            if ext == ".png":
                return decode_png(source)
            elif ext == ".bmp":
                return decode_bmp(source)
            else:
                raise RuntimeError(f"Format {ext} requires Pillow (`pip install pillow`). Fallback only supports PNG and BMP.")

    if HAS_PIL:
        if max_dimension and max(img.size) > max_dimension:
            scale = max_dimension / max(img.size)
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            img = img.resize(new_size, PILImage.Resampling.LANCZOS)

        w, h = img.size
        # Alpha compositing on white
        bg = PILImage.new("RGBA", (w, h), (255, 255, 255, 255))
        composite = PILImage.alpha_composite(bg, img).convert("RGB")
        raw_data = list(composite.getdata())
        pixels = [raw_data[y * w:(y + 1) * w] for y in range(h)]
        return w, h, pixels


# ==============================================================================
# 2. COLOR QUANTIZATION, CLUSTERING & THRESHOLDING
# ==============================================================================

def rgb_to_hex(rgb):
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def color_dist_sq(c1, c2):
    dr = c1[0] - c2[0]
    dg = c1[1] - c2[1]
    db = c1[2] - c2[2]
    return dr * dr + dg * dg + db * db


def quantize_kmeans(pixels, width, height, k=8, max_iter=6):
    """Fast K-Means color quantization with grid-binning initialization."""
    # Step 1: Subsample pixels into 12-bit RGB buckets
    bins = defaultdict(int)
    step_y = max(1, height // 120)
    step_x = max(1, width // 120)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            r, g, b = pixels[y][x]
            key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
            bins[key] += 1

    # Pick initial centroids from most frequent bins
    sorted_bins = sorted(bins.items(), key=lambda item: item[1], reverse=True)
    centroids = []
    for key, _ in sorted_bins:
        r = ((key >> 8) & 0xF) * 17
        g = ((key >> 4) & 0xF) * 17
        b = (key & 0xF) * 17
        # Ensure diverse centroids
        if not any(color_dist_sq((r, g, b), c) < 900 for c in centroids):
            centroids.append((r, g, b))
        if len(centroids) == k:
            break

    # Fill remainder if needed
    for key, _ in sorted_bins:
        if len(centroids) == k:
            break
        r = ((key >> 8) & 0xF) * 17
        g = ((key >> 4) & 0xF) * 17
        b = (key & 0xF) * 17
        if (r, g, b) not in centroids:
            centroids.append((r, g, b))

    if not centroids:
        centroids = [(0, 0, 0), (255, 255, 255)]

    # Subsampled pixel array for clustering
    sample_pts = []
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            sample_pts.append(pixels[y][x])

    # K-Means refinement iterations
    for _ in range(max_iter):
        clusters = [[] for _ in centroids]
        for p in sample_pts:
            best_idx = 0
            min_d = color_dist_sq(p, centroids[0])
            for i in range(1, len(centroids)):
                d = color_dist_sq(p, centroids[i])
                if d < min_d:
                    min_d = d
                    best_idx = i
            clusters[best_idx].append(p)

        new_centroids = []
        for i, cluster in enumerate(clusters):
            if cluster:
                avg_r = sum(p[0] for p in cluster) // len(cluster)
                avg_g = sum(p[1] for p in cluster) // len(cluster)
                avg_b = sum(p[2] for p in cluster) // len(cluster)
                new_centroids.append((avg_r, avg_g, avg_b))
            else:
                new_centroids.append(centroids[i])
        centroids = new_centroids

    # Map full image pixels to label indices
    label_grid = []
    for y in range(height):
        row = []
        for x in range(width):
            p = pixels[y][x]
            best_idx = 0
            min_d = color_dist_sq(p, centroids[0])
            for i in range(1, len(centroids)):
                d = color_dist_sq(p, centroids[i])
                if d < min_d:
                    min_d = d
                    best_idx = i
            row.append(best_idx)
        label_grid.append(row)

    return centroids, label_grid


def threshold_otsu(pixels, width, height):
    """Compute Otsu's optimal binarization threshold and label grid (0=bg, 1=fg)."""
    hist = [0] * 256
    total_pixels = width * height
    gray_grid = []

    for y in range(height):
        row = []
        for x in range(width):
            r, g, b = pixels[y][x]
            lum = int(0.299 * r + 0.587 * g + 0.114 * b)
            hist[lum] += 1
            row.append(lum)
        gray_grid.append(row)

    sum_all = sum(i * hist[i] for i in range(256))
    weight_bg = 0
    sum_bg = 0
    max_variance = -1.0
    threshold = 128

    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0:
            continue
        weight_fg = total_pixels - weight_bg
        if weight_fg == 0:
            break

        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg

        var_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var_between > max_variance:
            max_variance = var_between
            threshold = t

    # Invert so dark strokes = foreground (1)
    label_grid = []
    for y in range(height):
        row = [1 if gray_grid[y][x] < threshold else 0 for x in range(width)]
        label_grid.append(row)

    centroids = [(255, 255, 255), (0, 0, 0)]  # 0: white bg, 1: black fg
    return centroids, label_grid, threshold


def denoise_label_grid(grid, width, height):
    """Remove 1-pixel isolated noise using 3x3 modal filter."""
    denoised = [row[:] for row in grid]
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            curr = grid[y][x]
            # 8-neighbor counts
            counts = defaultdict(int)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    counts[grid[y + dy][x + dx]] += 1
            # If current pixel is severely outnumbered (<= 2 out of 9)
            if counts[curr] <= 2:
                majority = max(counts.items(), key=lambda it: it[1])[0]
                denoised[y][x] = majority
    return denoised


# ==============================================================================
# 3. DIRECTED EULERIAN BOUNDARY TRACING
# ==============================================================================

def trace_contours(binary_mask, width, height):
    """
    Trace directed boundary edges of foreground pixels (1) on lattice grid.
    Returns list of closed coordinate loops [(x, y), ...].
    """
    # Build directed edge adjacency graph
    # Clockwise boundary: foreground is on right of directed edge
    adj = defaultdict(list)
    for y in range(height):
        for x in range(width):
            if not binary_mask[y][x]:
                continue
            # Top edge: (x, y) -> (x+1, y)
            if y == 0 or not binary_mask[y - 1][x]:
                adj[(x, y)].append((x + 1, y))
            # Right edge: (x+1, y) -> (x+1, y+1)
            if x == width - 1 or not binary_mask[y][x + 1]:
                adj[(x + 1, y)].append((x + 1, y + 1))
            # Bottom edge: (x+1, y+1) -> (x, y+1)
            if y == height - 1 or not binary_mask[y + 1][x]:
                adj[(x + 1, y + 1)].append((x, y + 1))
            # Left edge: (x, y+1) -> (x, y)
            if x == 0 or not binary_mask[y][x - 1]:
                adj[(x, y + 1)].append((x, y))

    if not adj:
        return []

    # Disambiguation score: prefer sharpest right turn at saddle vertices
    def turn_score(vin, vout):
        cross = vin[0] * vout[1] - vin[1] * vout[0]
        dot = vin[0] * vout[0] + vin[1] * vout[1]
        return -math.atan2(cross, dot)

    visited_edges = set()
    contours = []

    for start_node, neighbors in list(adj.items()):
        for first_target in neighbors:
            edge = (start_node, first_target)
            if edge in visited_edges:
                continue

            loop = [start_node]
            visited_edges.add(edge)
            prev = start_node
            curr = first_target

            while curr != start_node:
                loop.append(curr)
                c_targets = [nb for nb in adj[curr] if (curr, nb) not in visited_edges]
                if not c_targets:
                    break

                vin = (curr[0] - prev[0], curr[1] - prev[1])
                # Sort candidate next steps by rightmost turn
                c_targets.sort(key=lambda nb: turn_score(vin, (nb[0] - curr[0], nb[1] - curr[1])))

                next_nb = c_targets[0]
                visited_edges.add((curr, next_nb))
                prev = curr
                curr = next_nb

            if curr == start_node and len(loop) >= 3:
                contours.append(loop)

    return contours


def signed_polygon_area(points):
    """Compute polygon signed area via Shoelace formula. Positive=CW, Negative=CCW."""
    n = len(points)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1] - points[j][0] * points[i][1]
    return area / 2.0


# ==============================================================================
# 4. PATH SIMPLIFICATION (COLLINEAR + RAMER-DOUGLAS-PEUCKER)
# ==============================================================================

def prune_collinear(points):
    """Remove redundant points on perfectly straight line segments."""
    n = len(points)
    if n <= 3:
        return points

    pruned = []
    for i in range(n):
        p_prev = points[(i - 1) % n]
        p_curr = points[i]
        p_next = points[(i + 1) % n]

        dx1 = p_curr[0] - p_prev[0]
        dy1 = p_curr[1] - p_prev[1]
        dx2 = p_next[0] - p_curr[0]
        dy2 = p_next[1] - p_curr[1]

        # Cross product == 0 and same direction (dot > 0)
        cross = dx1 * dy2 - dy1 * dx2
        dot = dx1 * dx2 + dy1 * dy2
        if cross == 0 and dot > 0:
            continue
        pruned.append(p_curr)

    return pruned if len(pruned) >= 3 else points


def rdp_open_line(points, epsilon):
    """Recursive Ramer-Douglas-Peucker for an open polyline."""
    if len(points) <= 2:
        return points

    p1 = points[0]
    p2 = points[-1]
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    line_len = math.hypot(dx, dy)

    max_dist = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        p = points[i]
        if line_len < 1e-9:
            dist = math.hypot(p[0] - p1[0], p[1] - p1[1])
        else:
            dist = abs(dy * p[0] - dx * p[1] + p2[0] * p1[1] - p2[1] * p1[0]) / line_len
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist > epsilon:
        left = rdp_open_line(points[:index + 1], epsilon)
        right = rdp_open_line(points[index:], epsilon)
        return left[:-1] + right
    else:
        return [p1, p2]


def optimize_polygon_chords(pts, tolerance=2.0):
    """
    Greedy longest-chord shortcutting across pixel-grid staircase noise.
    Finds the farthest forward index j such that all intermediate lattice points
    lie within perpendicular distance <= tolerance of the direct chord (i -> j).
    Bridges long straight diagonal edges with single linear segments, eliminating
    pixel staircase serrations.
    """
    n = len(pts)
    if n <= 3:
        return pts

    def is_chord_valid(i, j):
        p1, p2 = pts[i], pts[j]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        l = math.hypot(dx, dy)
        if l < 1e-9:
            return True
        dist_ahead = (j - i) if j >= i else (j + n - i)
        for k_step in range(1, dist_ahead):
            k = (i + k_step) % n
            pk = pts[k]
            d = abs(dy * pk[0] - dx * pk[1] + p2[0] * p1[1] - p2[1] * p1[0]) / l
            if d > tolerance:
                return False
        return True

    result = []
    i = 0
    while i < n:
        result.append(pts[i])
        last_valid = i + 1
        step = 2
        while True:
            target = i + step
            if target >= n:
                target = n - 1
            if target == last_valid:
                break
            if is_chord_valid(i, target):
                last_valid = target
                if target == n - 1:
                    break
                step *= 2
            else:
                low = last_valid
                high = target
                while low + 1 < high:
                    mid = (low + high) // 2
                    if is_chord_valid(i, mid):
                        low = mid
                    else:
                        high = mid
                last_valid = low
                break
        if last_valid <= i:
            break
        i = last_valid

    if len(result) > 3:
        d_close = math.hypot(result[0][0] - result[-1][0], result[0][1] - result[-1][1])
        if d_close < tolerance:
            result.pop()

    return prune_collinear(result) if len(result) >= 3 else pts


def simplify_polygon(points, epsilon=1.0):
    """Simplify closed polygon using greedy chord shortcutting + diameter RDP + collinear pruning."""
    if epsilon <= 0.0 or len(points) <= 4:
        return prune_collinear(points)

    # Pass 1: Greedy longest-chord shortcutting directly on raw lattice contour
    chord_tol = max(2.5, epsilon * 1.8)
    points = optimize_polygon_chords(points, tolerance=chord_tol)

    # Pass 2: Prune redundant collinear points along straight runs
    points = prune_collinear(points)
    if len(points) <= 4:
        return points

    n = len(points)
    # Find true maximum diameter across full polygon
    step = max(1, n // 80)
    max_d = -1.0
    idx_a, idx_b = 0, n // 2
    for a in range(0, n, step):
        for b in range(a + step, n, step):
            d = (points[a][0] - points[b][0]) ** 2 + (points[a][1] - points[b][1]) ** 2
            if d > max_d:
                max_d = d
                idx_a, idx_b = a, b

    chain1 = points[idx_a:idx_b + 1]
    chain2 = points[idx_b:] + points[:idx_a + 1]

    simp1 = rdp_open_line(chain1, epsilon)
    simp2 = rdp_open_line(chain2, epsilon)

    combined = simp1[:-1] + simp2[:-1]

    # Multi-pass collinear cleanup along flat runs
    if len(combined) > 3:
        changed = True
        passes = 0
        while changed and len(combined) > 3 and passes < 3:
            changed = False
            passes += 1
            m = len(combined)
            cleaned = []
            for i in range(m):
                p_prev = combined[(i - 1) % m]
                p_curr = combined[i]
                p_next = combined[(i + 1) % m]

                dx = p_next[0] - p_prev[0]
                dy = p_next[1] - p_prev[1]
                line_len = math.hypot(dx, dy)
                if line_len < 1e-9:
                    changed = True
                    continue

                dist_perp = abs(dy * p_curr[0] - dx * p_curr[1] + p_next[0] * p_prev[1] - p_next[1] * p_prev[0]) / line_len
                if dist_perp < max(0.9, epsilon * 0.45):
                    changed = True
                    continue
                cleaned.append(p_curr)

            if len(cleaned) >= 3:
                combined = cleaned

    return combined if len(combined) >= 3 else points


# ==============================================================================
# 5. CURVE FITTING & BÉZIER SMOOTHING
# ==============================================================================

def polygon_to_svg_path(points, smooth=0.5, corner_threshold_deg=60.0, curve_mode="adaptive", scale=1.0, precision=2):
    """
    Convert polygon vertices into SVG path string.
    - If curve_mode == 'crisp' or smooth <= 0.01: emits straight L segments (clean, non-wavy).
    - If curve_mode == 'adaptive': straight edges stay straight, curved arcs stay smooth.
    - If curve_mode == 'smooth': fits cubic Béziers except at sharp corners.
    """
    n = len(points)
    if n < 3:
        return ""

    pts = [(p[0] * scale, p[1] * scale) for p in points]

    # 1. Strict straight lines mode
    if curve_mode in ("crisp", "lines", "polygon", "straight") or smooth <= 0.001:
        parts = [f"M {pts[0][0]:.{precision}f} {pts[0][1]:.{precision}f}"]
        for p in pts[1:]:
            parts.append(f"L {p[0]:.{precision}f} {p[1]:.{precision}f}")
        parts.append("Z")
        return " ".join(parts)

    def dist(p1, p2):
        return math.hypot(p2[0] - p1[0], p2[1] - p1[1])

    def norm(v):
        l = math.hypot(v[0], v[1])
        return (v[0] / l, v[1] / l) if l > 1e-9 else (0.0, 0.0)

    # 2. Identify sharp corners and turn directions
    thresh_rad = math.radians(corner_threshold_deg)
    is_sharp = [False] * n
    turn_angles = [0.0] * n
    cross_signs = [0] * n

    for i in range(n):
        p_prev = pts[(i - 1) % n]
        p_curr = pts[i]
        p_next = pts[(i + 1) % n]

        v_in = (p_curr[0] - p_prev[0], p_curr[1] - p_prev[1])
        v_out = (p_next[0] - p_curr[0], p_next[1] - p_curr[1])
        l_in, l_out = math.hypot(*v_in), math.hypot(*v_out)

        if l_in > 1e-9 and l_out > 1e-9:
            dot = (v_in[0] * v_out[0] + v_in[1] * v_out[1]) / (l_in * l_out)
            cross = v_in[0] * v_out[1] - v_in[1] * v_out[0]
            turn_angle = math.acos(max(-1.0, min(1.0, dot)))
            turn_angles[i] = turn_angle
            cross_signs[i] = 1 if cross > 0 else (-1 if cross < 0 else 0)
            if turn_angle > thresh_rad:
                is_sharp[i] = True

    # Compute tangent directions
    tangents = []
    for i in range(n):
        if is_sharp[i]:
            tangents.append(None)
        else:
            p_prev = pts[(i - 1) % n]
            p_next = pts[(i + 1) % n]
            tangents.append(norm((p_next[0] - p_prev[0], p_next[1] - p_prev[1])))

    # Emit segments
    parts = [f"M {pts[0][0]:.{precision}f} {pts[0][1]:.{precision}f}"]
    for i in range(n):
        p0 = pts[i]
        p1 = pts[(i + 1) % n]
        chord_len = dist(p0, p1)
        chord_dir = norm((p1[0] - p0[0], p1[1] - p0[1]))

        # In adaptive mode: classify straight edges vs genuine arcs
        if curve_mode == "adaptive":
            # Sharp corner on either end -> straight edge
            if is_sharp[i] or is_sharp[(i + 1) % n]:
                parts.append(f"L {p1[0]:.{precision}f} {p1[1]:.{precision}f}")
                continue

            # Nearly straight collinear line (< 5 degrees turn on both ends) -> straight edge
            if turn_angles[i] < 0.087 and turn_angles[(i + 1) % n] < 0.087:
                parts.append(f"L {p1[0]:.{precision}f} {p1[1]:.{precision}f}")
                continue

            # Alternating inflection / staircase ripple -> straight line (eliminates wavy artifacts!)
            if cross_signs[i] * cross_signs[(i + 1) % n] < 0:
                parts.append(f"L {p1[0]:.{precision}f} {p1[1]:.{precision}f}")
                continue

        # Otherwise curve
        t0 = tangents[i] if tangents[i] is not None else chord_dir
        t1 = tangents[(i + 1) % n] if tangents[(i + 1) % n] is not None else chord_dir

        handle_len = (chord_len / 3.0) * smooth
        cp1 = (p0[0] + t0[0] * handle_len, p0[1] + t0[1] * handle_len)
        cp2 = (p1[0] - t1[0] * handle_len, p1[1] - t1[1] * handle_len)

        if is_sharp[i] and is_sharp[(i + 1) % n]:
            parts.append(f"L {p1[0]:.{precision}f} {p1[1]:.{precision}f}")
        else:
            parts.append(
                f"C {cp1[0]:.{precision}f} {cp1[1]:.{precision}f}, "
                f"{cp2[0]:.{precision}f} {cp2[1]:.{precision}f}, "
                f"{p1[0]:.{precision}f} {p1[1]:.{precision}f}"
            )

    parts.append("Z")
    return " ".join(parts)


# ==============================================================================
# 6. VECTORIZER CORE ENGINE
# ==============================================================================

class ImageVectorizer:
    """Configurable raster image to SVG vectorizer."""

    def __init__(
        self,
        mode="color",
        num_colors=8,
        simplify_epsilon=1.0,
        smoothness=0.5,
        min_area=10.0,
        corner_threshold=60.0,
        curve_mode="adaptive",
        scale=1.0,
        viewbox_only=False,
        precision=2,
    ):
        self.mode = mode.lower()
        self.num_colors = max(2, int(num_colors))
        self.simplify_epsilon = float(simplify_epsilon)
        self.smoothness = max(0.0, min(1.0, float(smoothness)))
        self.min_area = float(min_area)
        self.corner_threshold = float(corner_threshold)
        self.curve_mode = curve_mode.lower() if curve_mode else "adaptive"
        self.scale = float(scale)
        self.viewbox_only = viewbox_only
        self.precision = int(precision)

    def vectorize(self, input_source, max_dimension=None):
        """
        Vectorize input image path or pixel matrix.
        Returns SVG XML string.
        """
        if isinstance(input_source, (str, bytes, bytearray)):
            width, height, pixels = load_image(input_source, max_dimension=max_dimension)
        elif isinstance(input_source, tuple) and len(input_source) == 3:
            width, height, pixels = input_source
        else:
            raise ValueError("input_source must be a filepath, raw bytes, or (width, height, pixels) tuple")

        # Step 1: Color Quantization
        if self.mode in ("bw", "monochrome", "binary"):
            centroids, label_grid, _ = threshold_otsu(pixels, width, height)
            active_labels = [1]  # Only foreground
        else:
            centroids, label_grid = quantize_kmeans(pixels, width, height, k=self.num_colors)
            # Rank labels by total area descending (background rendered first)
            label_counts = defaultdict(int)
            for row in label_grid:
                for lbl in row:
                    label_counts[lbl] += 1
            active_labels = [lbl for lbl, _ in sorted(label_counts.items(), key=lambda it: it[1], reverse=True)]

        # Step 2: Denoise
        label_grid = denoise_label_grid(label_grid, width, height)

        # Step 3: Layer Contours & SVG Path Assembly
        svg_width = int(width * self.scale)
        svg_height = int(height * self.scale)
        viewbox = f"0 0 {svg_width} {svg_height}"

        svg_layers = []

        for lbl in active_labels:
            color = centroids[lbl]
            hex_color = rgb_to_hex(color)

            # Build binary mask
            mask = [[1 if label_grid[y][x] == lbl else 0 for x in range(width)] for y in range(height)]

            # Trace boundary contours
            contours = trace_contours(mask, width, height)
            if not contours:
                continue

            # Filter small areas & simplify
            path_segments = []
            for cnt in contours:
                area = signed_polygon_area(cnt)
                if abs(area) < self.min_area:
                    continue

                simplified = simplify_polygon(cnt, epsilon=self.simplify_epsilon)
                if len(simplified) < 3:
                    continue

                path_str = polygon_to_svg_path(
                    simplified,
                    smooth=self.smoothness,
                    corner_threshold_deg=self.corner_threshold,
                    curve_mode=self.curve_mode,
                    scale=self.scale,
                    precision=self.precision,
                )
                if path_str:
                    path_segments.append(path_str)

            if not path_segments:
                continue

            combined_d = " ".join(path_segments)

            if self.mode == "outline":
                svg_layers.append(
                    f'  <g id="layer-{lbl}" stroke="{hex_color}" fill="none" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round">\n'
                    f'    <path fill-rule="evenodd" d="{combined_d}" />\n'
                    f'  </g>'
                )
            else:
                svg_layers.append(
                    f'  <g id="layer-{lbl}" fill="{hex_color}">\n'
                    f'    <path fill-rule="evenodd" d="{combined_d}" />\n'
                    f'  </g>'
                )

        # Assemble full SVG document
        size_attrs = f'viewBox="{viewbox}"'
        if not self.viewbox_only:
            size_attrs = f'width="{svg_width}" height="{svg_height}" ' + size_attrs

        svg_content = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" {size_attrs}>\n'
            f'  <desc>Generated by Antigravity Python Vectorizer</desc>\n'
            + "\n".join(svg_layers) + "\n"
            f'</svg>\n'
        )
        return svg_content


def vectorize_image(
    input_path,
    output_path=None,
    mode="color",
    colors=8,
    simplify=1.0,
    smooth=0.5,
    min_area=10.0,
    corner_threshold=60.0,
    curve_mode="adaptive",
    scale=1.0,
    max_dimension=None,
    viewbox_only=False,
):
    """Convenience helper to vectorize image and save/return SVG."""
    vec = ImageVectorizer(
        mode=mode,
        num_colors=colors,
        simplify_epsilon=simplify,
        smoothness=smooth,
        min_area=min_area,
        corner_threshold=corner_threshold,
        curve_mode=curve_mode,
        scale=scale,
        viewbox_only=viewbox_only,
    )
    svg_output = vec.vectorize(input_path, max_dimension=max_dimension)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_output)

    return svg_output


# ==============================================================================
# 7. COMMAND LINE INTERFACE
# ==============================================================================

def parse_cli():
    parser = argparse.ArgumentParser(
        description="Custom Python Image Vectorizer: Converts raster images (PNG, JPEG, BMP) into scalable SVGs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 vectorizer.py photo.png -o vector.svg --colors 12
  python3 vectorizer.py logo.png -o logo.svg --mode bw --simplify 1.5 --smooth 0.6
  python3 vectorizer.py icon.bmp -o icon.svg --mode outline --min-area 20
""",
    )
    parser.add_argument("input", help="Path to input image file (PNG, JPG, BMP, WebP)")
    parser.add_argument("-o", "--output", help="Path to destination SVG file (defaults to <input>.svg)")
    parser.add_argument(
        "-m", "--mode",
        choices=["color", "bw", "monochrome", "outline"],
        default="color",
        help="Vectorization mode: 'color' (multi-layer), 'bw' (binary silhouette), 'outline' (edge strokes)",
    )
    parser.add_argument(
        "-c", "--colors",
        type=int,
        default=8,
        help="Number of quantized colors for color mode (default: 8)",
    )
    parser.add_argument(
        "-s", "--simplify",
        type=float,
        default=1.0,
        help="Ramer-Douglas-Peucker simplification tolerance in pixels (default: 1.0, 0 = disable)",
    )
    parser.add_argument(
        "--smooth",
        type=float,
        default=0.5,
        help="Cubic Bézier curve smoothing tension: 0.0 (sharp polygons) to 1.0 (ultra-smooth, default: 0.5)",
    )
    parser.add_argument(
        "-a", "--min-area",
        type=float,
        default=10.0,
        help="Minimum pixel area threshold to keep contour, filtering noise specks (default: 10.0)",
    )
    parser.add_argument(
        "--corner-threshold",
        type=float,
        default=60.0,
        help="Turn angle in degrees above which a corner is preserved sharp (default: 60.0)",
    )
    parser.add_argument(
        "--curve-mode",
        choices=["adaptive", "crisp", "smooth"],
        default="adaptive",
        help="Curvature mode: 'adaptive' (auto-detect lines vs arcs), 'crisp' (100%% straight lines, no wavy wiggles), 'smooth' (splines)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Output coordinate scale factor (default: 1.0)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=None,
        help="Max width/height pixel dimension to resize large images before vectorizing (speeds up processing)",
    )
    parser.add_argument(
        "--viewbox-only",
        action="store_true",
        help="Omit width/height attributes for fully responsive SVG viewBox",
    )
    return parser.parse_args()


def main():
    args = parse_cli()
    if not os.path.exists(args.input):
        print(f"Error: Input file '{args.input}' not found.", file=sys.stderr)
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base, _ = os.path.splitext(args.input)
        output_path = f"{base}.svg"

    print(f"Vectorizing: {args.input}")
    print(f"Parameters: mode={args.mode}, colors={args.colors}, simplify={args.simplify}, smooth={args.smooth}")

    svg_data = vectorize_image(
        input_path=args.input,
        output_path=output_path,
        mode=args.mode,
        colors=args.colors,
        simplify=args.simplify,
        smooth=args.smooth,
        min_area=args.min_area,
        corner_threshold=args.corner_threshold,
        curve_mode=args.curve_mode,
        scale=args.scale,
        max_dimension=args.max_size,
        viewbox_only=args.viewbox_only,
    )

    file_size_kb = len(svg_data.encode("utf-8")) / 1024.0
    print(f"Success: Wrote SVG to {output_path} ({file_size_kb:.2f} KB)")


if __name__ == "__main__":
    main()
