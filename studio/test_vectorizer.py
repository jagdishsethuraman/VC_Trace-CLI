#!/usr/bin/env python3
"""
Test Suite for Custom Python Image Vectorizer
Tests:
- Ramer-Douglas-Peucker (RDP) simplification
- Signed polygon area & hole detection
- Directed Eulerian boundary tracing
- Bézier curve fitting with sharp corner preservation
- End-to-end vectorization (monochrome, color, outline)
- PNG and BMP pure-python decoding
"""

import os
import sys
import math
import struct
import zlib
import unittest

from vectorizer import (
    prune_collinear,
    rdp_open_line,
    simplify_polygon,
    signed_polygon_area,
    trace_contours,
    polygon_to_svg_path,
    ImageVectorizer,
    vectorize_image,
    decode_png,
    decode_bmp,
)


class TestVectorizerAlgorithms(unittest.TestCase):

    def test_collinear_pruning(self):
        # Stepped straight line
        pts = [(0, 0), (1, 0), (2, 0), (3, 0), (3, 1), (3, 2)]
        pruned = prune_collinear(pts)
        self.assertEqual(pruned, [(0, 0), (3, 0), (3, 2)])

    def test_rdp_simplification(self):
        pts = [(0, 0), (1, 0.1), (2, -0.1), (3, 0), (4, 4)]
        simplified = rdp_open_line(pts, epsilon=0.5)
        self.assertEqual(simplified, [(0, 0), (3, 0), (4, 4)])

    def test_signed_area_and_orientation(self):
        # Clockwise box 10x10
        cw_box = [(0, 0), (10, 0), (10, 10), (0, 10)]
        area_cw = signed_polygon_area(cw_box)
        self.assertEqual(area_cw, 100.0)

        # Counter-clockwise box (hole)
        ccw_box = [(0, 10), (10, 10), (10, 0), (0, 0)]
        area_ccw = signed_polygon_area(ccw_box)
        self.assertEqual(area_ccw, -100.0)

    def test_contour_tracing_with_hole(self):
        # 6x6 grid with 4x4 foreground and 2x2 hole
        grid = [
            [0, 0, 0, 0, 0, 0],
            [0, 1, 1, 1, 1, 0],
            [0, 1, 0, 0, 1, 0],
            [0, 1, 0, 0, 1, 0],
            [0, 1, 1, 1, 1, 0],
            [0, 0, 0, 0, 0, 0],
        ]
        contours = trace_contours(grid, 6, 6)
        self.assertEqual(len(contours), 2)

        areas = [signed_polygon_area(c) for c in contours]
        areas.sort(reverse=True)
        # Outer: 4x4 = 16.0; Inner hole: -2x-2 = -4.0
        self.assertAlmostEqual(areas[0], 16.0)
        self.assertAlmostEqual(areas[1], -4.0)

    def test_sharp_corner_preservation(self):
        # 90-degree square
        box = [(0, 0), (20, 0), (20, 20), (0, 20)]
        path_str = polygon_to_svg_path(box, smooth=0.5, corner_threshold_deg=60.0)
        # All 4 corners should be detected sharp (turn = 90 deg > 60 deg)
        # Sharp corners emit linear L segments
        self.assertIn("L", path_str)
        self.assertIn("M", path_str)
        self.assertIn("Z", path_str)

    def test_smooth_spline_generation(self):
        # Circle polygon (16 vertices, ~22.5 deg turn < 60 deg)
        circle_pts = []
        for i in range(16):
            theta = 2.0 * math.pi * i / 16
            circle_pts.append((25.0 + 20.0 * math.cos(theta), 25.0 + 20.0 * math.sin(theta)))

        path_str = polygon_to_svg_path(circle_pts, smooth=0.5, corner_threshold_deg=60.0)
        # Smooth circle should emit Cubic Bézier 'C' segments
        self.assertIn("C", path_str)
        self.assertIn("M", path_str)
        self.assertIn("Z", path_str)


class TestVectorizerEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = "/tmp/vectorizer_test_assets"
        os.makedirs(cls.test_dir, exist_ok=True)

        # 1. Synthesize PNG: Red circle on white bg with black ring
        w, h = 48, 48
        raw = bytearray()
        for y in range(h):
            raw.append(0)  # filter None
            for x in range(w):
                dx = x - 24
                dy = y - 24
                r_sq = dx * dx + dy * dy
                if r_sq <= 100:  # Inner red circle
                    raw.extend([220, 20, 60])
                elif r_sq <= 225:  # Black ring
                    raw.extend([20, 20, 20])
                else:  # White background
                    raw.extend([255, 255, 255])

        compressed = zlib.compress(bytes(raw))
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        png_bytes = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
        cls.sample_png = os.path.join(cls.test_dir, "circle_ring.png")
        with open(cls.sample_png, "wb") as f:
            f.write(png_bytes)

        # 2. Synthesize BMP: Dual color stripes
        row_bytes = (w * 3 + 3) & ~3
        px_size = row_bytes * h
        bmp_hdr = struct.pack("<2sIHHI", b"BM", 54 + px_size, 0, 0, 54)
        dib_hdr = struct.pack("<IIIHHIIIIII", 40, w, h, 1, 24, 0, px_size, 2835, 2835, 0, 0)
        px_bytes = bytearray(px_size)
        for y in range(h):
            for x in range(w):
                offset = y * row_bytes + x * 3
                if x < 24:
                    px_bytes[offset:offset + 3] = b"\x00\xa5\xff"  # Gold (BGR)
                else:
                    px_bytes[offset:offset + 3] = b"\x80\x00\x00"  # Navy (BGR)

        cls.sample_bmp = os.path.join(cls.test_dir, "stripes.bmp")
        with open(cls.sample_bmp, "wb") as f:
            f.write(bmp_hdr + dib_hdr + px_bytes)

    def test_pure_python_decoders(self):
        w_png, h_png, px_png = decode_png(self.sample_png)
        self.assertEqual((w_png, h_png), (48, 48))
        self.assertEqual(px_png[24][24], (220, 20, 60))

        w_bmp, h_bmp, px_bmp = decode_bmp(self.sample_bmp)
        self.assertEqual((w_bmp, h_bmp), (48, 48))
        self.assertEqual(px_bmp[0][0], (255, 165, 0))

    def test_vectorize_color_png(self):
        out_svg = os.path.join(self.test_dir, "circle_ring.svg")
        svg_str = vectorize_image(
            input_path=self.sample_png,
            output_path=out_svg,
            mode="color",
            colors=3,
            simplify=0.8,
            smooth=0.6,
        )
        self.assertTrue(os.path.exists(out_svg))
        self.assertIn("<svg", svg_str)
        self.assertIn("fill-rule=\"evenodd\"", svg_str)
        self.assertIn("</svg>", svg_str)
        self.assertGreater(len(svg_str), 100)

    def test_vectorize_monochrome_mode(self):
        out_svg = os.path.join(self.test_dir, "circle_mono.svg")
        svg_str = vectorize_image(
            input_path=self.sample_png,
            output_path=out_svg,
            mode="bw",
            simplify=1.0,
            smooth=0.5,
        )
        self.assertTrue(os.path.exists(out_svg))
        self.assertIn("<svg", svg_str)
        self.assertIn("fill=\"#000000\"", svg_str)

    def test_vectorize_outline_mode(self):
        out_svg = os.path.join(self.test_dir, "stripes_outline.svg")
        svg_str = vectorize_image(
            input_path=self.sample_bmp,
            output_path=out_svg,
            mode="outline",
            colors=2,
            simplify=1.0,
        )
        self.assertTrue(os.path.exists(out_svg))
        self.assertIn("stroke=", svg_str)
        self.assertIn("fill=\"none\"", svg_str)


if __name__ == "__main__":
    unittest.main()
