#!/usr/bin/env python3
"""
Browzwear VStitcher Artwork Vectorization Plugin & Bridge
---------------------------------------------------------
Vectorizes raster logos, prints, decals, and sketches into clean CAD SVGs
using VectorCraft headless CLI subprocess.

Features for Apparel CAD & 3D Simulation:
- Overlapping layer method to eliminate 1px white hairline seams on 3D fabric drape.
- Native geometric shape detection (circles, ellipses, rects) for ultra-light 3D mesh tessellation.
- Centerline tracing for single-line laser cut lines and embroidery running guides.
- DPI-aware metric scaling (mm/in) for 1:1 physical pattern alignment.
- Non-blocking external process: zero VStitcher UI freeze.
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path


def find_vectorcraft_cli():
    """Locate the vectorizer CLI binary (prioritizing 2.4MB vc-trace-mini, falling back to vectorcraft-cli)."""
    # 1. First priority: Lightweight 2.4MB vc-trace-mini
    mini_candidates = [
        Path(__file__).resolve().parent / "bin" / "vc-trace-mini",
        Path(__file__).resolve().parent.parent / "bin" / "vc-trace-mini",
        Path(__file__).resolve().parent.parent / "vectorcraft" / "target" / "release" / "vc-trace-mini",
        Path.home() / "Documents" / "Developer" / "vectorcraft" / "target" / "release" / "vc-trace-mini",
    ]
    for c in mini_candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c), True

    import shutil
    sys_mini = shutil.which("vc-trace-mini")
    if sys_mini:
        return sys_mini, True

    # 2. Fallback: Full 59MB vectorcraft-cli
    full_candidates = [
        Path(__file__).resolve().parent / "bin" / "vectorcraft-cli",
        Path(__file__).resolve().parent.parent / "bin" / "vectorcraft-cli",
        Path(__file__).resolve().parent.parent / "vectorcraft" / "target" / "release" / "vectorcraft-cli",
        Path.home() / "Documents" / "Developer" / "vectorcraft" / "target" / "release" / "vectorcraft-cli",
    ]
    for c in full_candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c), False

    sys_full = shutil.which("vectorcraft-cli")
    if sys_full:
        return sys_full, False

    raise FileNotFoundError(
        "No vectorizer binary found (checked vc-trace-mini and vectorcraft-cli). "
        "Build with 'cargo build --release -p vc-trace-mini'."
    )


def trace_for_vstitcher(
    input_path: str,
    output_path: str = None,
    preset: str = "Flat Logo",
    colors: int = 8,
    ignore_white: bool = True,
    overlapping: bool = True,
    shapes: bool = True,
    snap_curves: bool = True,
    strokes: bool = False,
    stroke_width: float = 10.0,
    noise: int = 10,
    target_width_mm: float = None,
) -> dict:
    """
    Vectorize artwork for Browzwear VStitcher.

    Args:
        input_path: Path to raster artwork (PNG, JPG, BMP, TIFF, WebP, PSD)
        output_path: Destination SVG path (defaults to <input_stem>_vstitcher.svg)
        preset: 'Flat Logo', '6 Colors', '16 Colors', 'Black and White Logo', 'Line Art', 'Silhouettes'
        colors: Number of color quantization clusters (for multi-color presets)
        ignore_white: Remove white background for transparent artwork decals
        overlapping: Overlap color boundaries to eliminate 3D fabric drape gaps
        shapes: Snap recognized circles, ellipses, and rectangles into native CAD primitives
        snap_curves: Snap near-straight chords into straight lines
        strokes: Centerline tracing for single-line embroidery/laser contours
        stroke_width: Pixel width cutoff for centerline extraction
        noise: Pixel area noise threshold (u32)
        target_width_mm: Physical width in mm to inject into SVG viewBox for 1:1 pattern scaling

    Returns:
        dict: Execution telemetry (paths, anchors, duration_ms, output_path)
    """
    cli_path, is_mini = find_vectorcraft_cli()
    in_file = Path(input_path).resolve()
    if not in_file.is_file():
        raise FileNotFoundError(f"Input file not found: {in_file}")

    if not output_path:
        output_path = str(in_file.parent / f"{in_file.stem}_vstitcher.svg")
    out_file = Path(output_path).resolve()

    import time
    t0 = time.perf_counter()
    anchors = 0
    paths = 0

    if is_mini:
        cmd = [
            cli_path,
            "-i", str(in_file),
            "-o", str(out_file),
            "-p", preset,
            "--colors", str(int(colors)),
            "--noise", str(int(noise)),
            "--overlapping" if overlapping else "--abutting",
            "--json"
        ]
        if ignore_white:
            cmd.append("--ignore-white")
        else:
            cmd.append("--no-ignore-white")
        if shapes:
            cmd.append("--shapes")
        if snap_curves:
            cmd.append("--snap")
        if strokes:
            cmd.extend(["--strokes", "--stroke-width", str(float(stroke_width))])

        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        try:
            meta = json.loads(proc.stdout)
            anchors = meta.get("anchorCount", 0)
            paths = meta.get("pathCount", 0)
        except Exception:
            pass
    else:
        params_dict = {
            "preset": preset,
            "params": {
                "ignoreWhite": bool(ignore_white),
                "method": "overlapping" if overlapping else "abutting",
                "shapes": bool(shapes),
                "snapCurvesToLines": bool(snap_curves),
                "noise": int(noise),
            }
        }
        if preset in ("3 Colors", "6 Colors", "16 Colors", "Color", "Auto Color"):
            params_dict["params"]["colors"] = int(colors)
        if strokes:
            params_dict["params"]["strokes"] = True
            params_dict["params"]["strokeWidth"] = float(stroke_width)

        cmd = [
            cli_path, "run",
            "--in", str(in_file),
            "--cmd", "select.all",
            "--cmd", "imageTrace.makeAndExpand",
            "--params", json.dumps(params_dict),
            "--export", str(out_file)
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        for line in proc.stdout.splitlines():
            try:
                data = json.loads(line)
                if data.get("step") == "cmd" and data.get("command") == "imageTrace.makeAndExpand":
                    res = data.get("result", {})
                    anchors = res.get("anchors", 0)
                    paths = res.get("paths", 0)
            except Exception:
                pass

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Optional metric physical scaling injection for Browzwear VStitcher
    if target_width_mm and out_file.is_file():
        with open(out_file, "r", encoding="utf-8") as f:
            svg_text = f.read()
        import re
        viewbox_match = re.search(r'viewBox="0 0 ([\d\.]+) ([\d\.]+)"', svg_text)
        if viewbox_match:
            vb_w = float(viewbox_match.group(1))
            vb_h = float(viewbox_match.group(2))
            aspect = vb_h / max(vb_w, 1.0)
            target_h_mm = target_width_mm * aspect
            svg_text = re.sub(
                r'<svg ([^>]+)>',
                f'<svg width="{target_width_mm:.2f}mm" height="{target_h_mm:.2f}mm" \\1>',
                svg_text,
                count=1
            )
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(svg_text)

    return {
        "success": True,
        "input": str(in_file),
        "output": str(out_file),
        "paths": paths,
        "anchors": anchors,
        "duration_ms": round(elapsed_ms, 2),
        "preset": preset,
    }


def main():
    parser = argparse.ArgumentParser(description="VStitcher Artwork Vectorization CLI Bridge")
    parser.add_argument("input", help="Input artwork image path (PNG, JPG, BMP, TIFF)")
    parser.add_argument("-o", "--output", help="Output SVG path")
    parser.add_argument("-p", "--preset", default="Flat Logo", help="Tracing preset (default: Flat Logo)")
    parser.add_argument("-c", "--colors", type=int, default=8, help="Color count (default: 8)")
    parser.add_argument("--keep-white", action="store_true", help="Keep white background (default strips white)")
    parser.add_argument("--abutting", action="store_true", help="Abutting edges instead of overlapping")
    parser.add_argument("--centerline", action="store_true", help="Centerline stroked tracing for lines/embroidery")
    parser.add_argument("--stroke-width", type=float, default=10.0, help="Centerline stroke width (px)")
    parser.add_argument("--width-mm", type=float, help="Target physical garment artwork width in mm")

    args = parser.parse_args()

    try:
        res = trace_for_vstitcher(
            input_path=args.input,
            output_path=args.output,
            preset=args.preset,
            colors=args.colors,
            ignore_white=not args.keep_white,
            overlapping=not args.abutting,
            strokes=args.centerline,
            stroke_width=args.stroke_width,
            target_width_mm=args.width_mm,
        )
        print("\n" + "=" * 55)
        print("  ⚡ VStitcher Artwork Vectorization Complete")
        print("=" * 55)
        print(f"  Input:    {res['input']}")
        print(f"  Output:   {res['output']}")
        print(f"  Paths:    {res['paths']}")
        print(f"  Anchors:  {res['anchors']}")
        print(f"  Duration: {res['duration_ms']} ms")
        print("=" * 55 + "\n")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
