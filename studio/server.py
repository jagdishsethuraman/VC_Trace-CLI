#!/usr/bin/env python3
"""
Custom Python Image Vectorizer - Interactive Local Web Server
Zero-dependency server using Python standard library http.server.
Endpoints:
  GET  /              - Serves interactive UI (web/index.html)
  GET  /api/presets   - Returns 4 procedural base64 PNG test presets
  POST /api/vectorize - Vectorizes uploaded base64 raster image to SVG
"""

import os
import sys
import json
import base64
import time
import socket
import struct
import zlib
import argparse
import subprocess
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Add parent directory to path so vectorizer is importable
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from vectorizer import ImageVectorizer, load_image

def get_vectorcraft_bin():
    """Find best available VectorCraft binary: vc-trace-mini (2.4MB) or vectorcraft-cli (59MB)."""
    mini_candidates = [
        os.path.abspath(os.path.join(CURRENT_DIR, "..", "bin", "vc-trace-mini")),
        os.path.abspath(os.path.join(CURRENT_DIR, "bin", "vc-trace-mini")),
        os.path.abspath(os.path.join(CURRENT_DIR, "..", "vectorcraft", "target", "release", "vc-trace-mini")),
        os.path.expanduser("~/Documents/Developer/vectorcraft/target/release/vc-trace-mini"),
    ]
    for c in mini_candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c, True

    full_candidates = [
        os.path.abspath(os.path.join(CURRENT_DIR, "..", "vectorcraft", "target", "release", "vectorcraft-cli")),
        os.path.abspath(os.path.join(CURRENT_DIR, "..", "bin", "vectorcraft-cli")),
        os.path.expanduser("~/Documents/Developer/vectorcraft/target/release/vectorcraft-cli"),
    ]
    for c in full_candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c, False

    return None, False


def run_vectorcraft(image_bytes, preset="Flat Logo", colors=8, ignore_white=True,
                    method="overlapping", shapes=True, snap_curves=True,
                    strokes=False, stroke_width=10, noise=10):
    """Execute VectorCraft native Rust vectorizer via headless CLI."""
    bin_path, is_mini = get_vectorcraft_bin()
    if not bin_path:
        raise FileNotFoundError("VectorCraft binary not found. Build with 'cargo build --release -p vc-trace-mini'.")

    # Map common aliases to built-in VectorCraft preset names
    preset_map = {
        "Black and White": "Black and White Logo",
        "Silhouette": "Silhouettes",
        "Monochrome": "Black and White Logo",
        "Default": "Default",
    }
    preset = preset_map.get(preset, preset)

    t0 = time.perf_counter()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as in_f:
        in_path = in_f.name
        in_f.write(image_bytes)

    out_path = in_path + ".svg"
    try:
        anchors = 0
        paths = 0

        if is_mini:
            cmd = [
                bin_path,
                "-i", in_path,
                "-o", out_path,
                "-p", preset,
                "--colors", str(int(colors)),
                "--noise", str(int(noise if noise is not None else 10)),
                "--overlapping" if method == "overlapping" else "--abutting",
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
                    "method": method if method in ("overlapping", "abutting") else "overlapping",
                    "shapes": bool(shapes),
                    "snapCurvesToLines": bool(snap_curves),
                }
            }
            if preset in ("3 Colors", "6 Colors", "16 Colors", "Color", "Auto Color"):
                params_dict["params"]["colors"] = int(colors)
            if noise is not None:
                params_dict["params"]["noise"] = int(noise)
            if strokes:
                params_dict["params"]["strokes"] = True
                params_dict["params"]["strokeWidth"] = float(stroke_width)

            cmd = [
                bin_path, "run",
                "--in", in_path,
                "--cmd", "select.all",
                "--cmd", "imageTrace.makeAndExpand",
                "--params", json.dumps(params_dict),
                "--export", out_path
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

        with open(out_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        c_count = svg_content.count(" C ")
        l_count = svg_content.count(" L ")
        m_count = svg_content.count("M ")
        total_segments = c_count + l_count + m_count

        return {
            "success": True,
            "svg": svg_content,
            "duration_ms": round(elapsed_ms, 2),
            "svg_bytes": len(svg_content.encode("utf-8")),
            "path_count": paths or svg_content.count("<path"),
            "anchor_count": anchors,
            "segment_count": total_segments,
            "engine": "vectorcraft",
        }
    finally:
        if os.path.exists(in_path):
            try: os.remove(in_path)
            except Exception: pass
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except Exception: pass


# ==============================================================================
# PROCEDURAL PRESET GENERATION (PNG ENCODED VIA ZLIB)
# ==============================================================================

def make_png_data_url(width, height, draw_fn):
    """Generate base64 data:image/png URL from procedural drawing callback."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter None
        for x in range(width):
            r, g, b = draw_fn(x, y, width, height)
            raw.extend([r, g, b])

    compressed = zlib.compress(bytes(raw))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png_bytes = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def generate_presets():
    """Generate 4 distinct test patterns for immediate testing."""
    presets = []

    # 1. Concentric Target Rings (tests nested circular holes and fill-rule=evenodd)
    def target_fn(x, y, w, h):
        cx, cy = w // 2, h // 2
        d_sq = (x - cx) ** 2 + (y - cy) ** 2
        r = d_sq ** 0.5
        if r < 14:
            return (220, 38, 38)     # Crimson center
        elif r < 28:
            return (255, 255, 255)   # White ring
        elif r < 44:
            return (37, 99, 235)     # Royal Blue ring
        elif r < 58:
            return (255, 255, 255)   # White ring
        elif r < 62:
            return (15, 23, 42)      # Dark Navy border
        return (248, 250, 252)       # Light background

    # 2. Geometric Crest & Shield (tests sharp angles vs curves)
    def crest_fn(x, y, w, h):
        cx, cy = w // 2, h // 2
        nx = abs(x - cx)
        ny = y
        # Shield boundary
        is_shield = (ny >= 16) and (nx <= 48) and (ny <= 64 or (ny - 64) * 1.5 + nx <= 48)
        # Inner star
        dx = x - cx
        dy = y - 48
        is_diamond = abs(dx) + abs(dy) <= 16
        if is_diamond:
            return (229, 169, 60)    # Atelier Brass
        elif is_shield:
            return (15, 23, 42)      # Obsidian Slate
        return (255, 255, 255)

    # 3. Script Monogram / S-Curve (tests smooth organic spline interpolation)
    def script_fn(x, y, w, h):
        cx, cy = w // 2, h // 2
        # Parametric S-curve ribbon
        dx = x - cx
        dy = y - cy
        expected_x = 24.0 * (dy / 40.0) ** 3 - 18.0 * (dy / 40.0)
        dist_to_curve = abs(dx - expected_x)
        if dist_to_curve < 7.0 and abs(dy) < 46:
            return (16, 185, 129)    # Emerald green
        return (255, 255, 255)

    # 4. Multi-Color Palette Diamonds (tests 4-color quantization and layer sorting)
    def palette_fn(x, y, w, h):
        cx, cy = w // 2, h // 2
        if x < cx and y < cy:
            return (239, 68, 68)     # Coral Red
        elif x >= cx and y < cy:
            return (59, 130, 246)    # Sky Blue
        elif x < cx and y >= cy:
            return (245, 158, 11)    # Amber Gold
        else:
            return (16, 185, 129)    # Mint Green

    presets.append({
        "id": "target",
        "name": "Target Rings",
        "desc": "Concentric rings & circular cutouts (tests holes & evenodd)",
        "mode": "color",
        "colors": 4,
        "simplify": 0.8,
        "smooth": 0.6,
        "image": make_png_data_url(128, 128, target_fn),
    })

    presets.append({
        "id": "crest",
        "name": "Geometric Crest",
        "desc": "Sharp corners & polygonal shield with center cutout",
        "mode": "color",
        "colors": 3,
        "simplify": 1.2,
        "smooth": 0.4,
        "image": make_png_data_url(128, 128, crest_fn),
    })

    presets.append({
        "id": "script",
        "name": "Script Monogram",
        "desc": "Cursive ribbon stroke (tests Catmull-Rom Bézier splines)",
        "mode": "bw",
        "colors": 2,
        "simplify": 1.0,
        "smooth": 0.8,
        "image": make_png_data_url(128, 128, script_fn),
    })

    presets.append({
        "id": "palette",
        "name": "Chromatic Blocks",
        "desc": "4-quadrant chromatic grid with sharp boundaries",
        "mode": "color",
        "colors": 4,
        "simplify": 1.5,
        "smooth": 0.0,
        "image": make_png_data_url(128, 128, palette_fn),
    })

    return presets


CACHED_PRESETS = generate_presets()


# ==============================================================================
# HTTP REQUEST HANDLER
# ==============================================================================

class VectorizerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Terse logging: suppress spammy 200 static logs
        if len(args) > 1 and str(args[1]) in ("200", "304") and not args[0].startswith("POST"):
            return
        super().log_message(format, *args)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/presets":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(CACHED_PRESETS).encode("utf-8"))
            return

        # Serve static web directory
        if path == "/" or path == "/index.html":
            file_path = os.path.join(CURRENT_DIR, "web", "index.html")
            content_type = "text/html; charset=utf-8"
        else:
            clean_rel = path.lstrip("/")
            file_path = os.path.join(CURRENT_DIR, "web", clean_rel)
            if file_path.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            elif file_path.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            elif file_path.endswith(".svg"):
                content_type = "image/svg+xml"
            elif file_path.endswith(".png"):
                content_type = "image/png"
            else:
                content_type = "application/octet-stream"

        if os.path.isfile(file_path):
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/vectorize":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            payload = json.loads(body.decode("utf-8"))

            raw_image = payload.get("image", "")
            if not raw_image:
                raise ValueError("Missing 'image' parameter in request body")

            # Strip data URL header if present
            if "," in raw_image:
                raw_image = raw_image.split(",", 1)[1]

            image_bytes = base64.b64decode(raw_image)

            # Engine selection: "vectorcraft" (default), "python", or "compare"
            engine = payload.get("engine", "vectorcraft")

            # VectorCraft Parameters
            vc_preset = payload.get("preset", "Flat Logo")
            vc_ignore_white = payload.get("ignore_white", True)
            vc_method = payload.get("method", "overlapping")
            vc_shapes = payload.get("shapes", True)
            vc_snap_curves = payload.get("snap_curves", True)
            vc_strokes = payload.get("strokes", False)
            vc_stroke_width = float(payload.get("stroke_width", 10.0))
            vc_noise = float(payload.get("noise", 10.0))

            # Python Parameters
            mode = payload.get("mode", "color")
            curve_mode = payload.get("curve_mode", "adaptive")
            colors = int(payload.get("colors", 8))
            simplify = float(payload.get("simplify", 1.0))
            smooth = float(payload.get("smooth", 0.5))
            min_area = float(payload.get("min_area", 10.0))
            corner_thresh = float(payload.get("corner_threshold", 60.0))
            scale = float(payload.get("scale", 1.0))
            max_size = payload.get("max_size")
            if max_size is not None:
                max_size = int(max_size)
            viewbox_only = bool(payload.get("viewbox_only", False))

            # Pre-read dimensions
            w, h, _ = load_image(image_bytes, max_dimension=max_size)

            def execute_python():
                t0 = time.perf_counter()
                vec = ImageVectorizer(
                    mode=mode,
                    num_colors=colors,
                    simplify_epsilon=simplify,
                    smoothness=smooth,
                    min_area=min_area,
                    corner_threshold=corner_thresh,
                    curve_mode=curve_mode,
                    scale=scale,
                    viewbox_only=viewbox_only,
                )
                svg_str = vec.vectorize(image_bytes, max_dimension=max_size)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                c_cnt = svg_str.count(" C ")
                l_cnt = svg_str.count(" L ")
                m_cnt = svg_str.count("M ")
                return {
                    "success": True,
                    "svg": svg_str,
                    "duration_ms": round(elapsed_ms, 2),
                    "svg_bytes": len(svg_str.encode("utf-8")),
                    "path_count": svg_str.count("<path"),
                    "anchor_count": (c_cnt * 3) + l_cnt + m_cnt,
                    "segment_count": c_cnt + l_cnt + m_cnt,
                    "raster_width": w,
                    "raster_height": h,
                    "engine": "python",
                }

            def execute_vectorcraft():
                res = run_vectorcraft(
                    image_bytes,
                    preset=vc_preset,
                    colors=colors,
                    ignore_white=vc_ignore_white,
                    method=vc_method,
                    shapes=vc_shapes,
                    snap_curves=vc_snap_curves,
                    strokes=vc_strokes,
                    stroke_width=vc_stroke_width,
                    noise=vc_noise,
                )
                res["raster_width"] = w
                res["raster_height"] = h
                return res

            if engine == "vectorcraft":
                response_data = execute_vectorcraft()
            elif engine == "python":
                response_data = execute_python()
            elif engine == "compare":
                vc_res = execute_vectorcraft()
                py_res = execute_python()
                speedup = round(py_res["duration_ms"] / max(vc_res["duration_ms"], 0.1), 1)
                py_anchors = py_res.get("anchor_count", py_res["segment_count"])
                vc_anchors = vc_res.get("anchor_count", vc_res["segment_count"])
                anchor_red = round(max(0, (py_anchors - vc_anchors) / max(py_anchors, 1) * 100), 1)

                response_data = {
                    "success": True,
                    "engine": "compare",
                    "vectorcraft": vc_res,
                    "python": py_res,
                    "svg": vc_res["svg"],
                    "duration_ms": vc_res["duration_ms"],
                    "svg_bytes": vc_res["svg_bytes"],
                    "path_count": vc_res["path_count"],
                    "anchor_count": vc_anchors,
                    "segment_count": vc_res["segment_count"],
                    "raster_width": w,
                    "raster_height": h,
                    "comparison": {
                        "speedup_ratio": speedup,
                        "anchor_reduction_pct": anchor_red,
                        "py_duration_ms": py_res["duration_ms"],
                        "vc_duration_ms": vc_res["duration_ms"],
                        "py_anchors": py_anchors,
                        "vc_anchors": vc_anchors,
                    }
                }
            else:
                raise ValueError(f"Unknown engine '{engine}'. Choose 'vectorcraft', 'python', or 'compare'.")

            resp_bytes = json.dumps(response_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(resp_bytes)

        except Exception as e:
            err_resp = json.dumps({"success": False, "error": str(e)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_resp)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(err_resp)


# ==============================================================================
# SERVER RUNNER
# ==============================================================================

def find_available_port(start_port=8088, max_attempts=15):
    """Scan for free port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start_port


def main():
    parser = argparse.ArgumentParser(description="Local Web Testing Server for Python Image Vectorizer")
    parser.add_argument("--port", type=int, default=8088, help="Port to bind server (default: 8088)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    args = parser.parse_args()

    port = find_available_port(args.port)
    server_address = (args.host, port)
    httpd = HTTPServer(server_address, VectorizerHandler)

    print(f"\n=======================================================")
    print(f"  ⚡ Python Image Vectorizer Test Studio")
    print(f"  🌐 URL: http://{args.host}:{port}")
    print(f"  📁 Root: {os.path.join(CURRENT_DIR, 'web')}")
    print(f"=======================================================\n")
    print(f"Ready. Press Ctrl+C to terminate server.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
