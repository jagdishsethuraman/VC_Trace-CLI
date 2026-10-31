# Custom Python Image Vectorizer (`py_vectorizer`)

High-performance, standalone raster-to-SVG vectorizer in Python. Converts PNG, JPEG, and BMP images into clean, layered, scalable SVG vectors.

## Key Features
- **Dual-Engine Image Loader**: Direct integration with Pillow (`PIL`) when available, plus zero-dependency built-in pure-Python PNG & BMP decoders for running anywhere.
- **Eulerian Boundary Graph**: Exact pixel-lattice directed-edge contour tracing with deterministic saddle-point resolution.
- **Topology & Holes**: Automatic signed Shoelace area orientation and SVG `fill-rule="evenodd"` compound paths.
- **Path Simplification**: Collinear vertex removal + Ramer-Douglas-Peucker (RDP) algorithm.
- **Cubic Bézier Curve Fitting**: Catmull-Rom to Bézier spline conversion with sharp corner preservation.
- **Modes**: Multi-color posterization (`color`), binary silhouette (`bw`), and stroke edge outlines (`outline`).

## CLI Usage

```bash
# Basic color vectorization
python3 vectorizer.py photo.png -o vector.svg --colors 8

# High-contrast monochrome logo / signature
python3 vectorizer.py signature.png -o signature.svg --mode bw --simplify 1.5 --smooth 0.6

# Wireframe / outline vectorization
python3 vectorizer.py artwork.bmp -o outlines.svg --mode outline --min-area 20

# Full options
python3 vectorizer.py image.png \
  --output result.svg \
  --mode color \
  --colors 12 \
  --simplify 1.2 \
  --smooth 0.5 \
  --min-area 15 \
  --corner-threshold 60.0 \
  --scale 1.0 \
  --viewbox-only
```

## Python API Usage

```python
from vectorizer import ImageVectorizer, vectorize_image

# 1. Quick helper
svg_content = vectorize_image(
    input_path="input.png",
    output_path="output.svg",
    mode="color",
    colors=8,
    simplify=1.0,
    smooth=0.5,
    min_area=10.0,
)

# 2. Configurable class
vectorizer = ImageVectorizer(
    mode="color",
    num_colors=8,
    simplify_epsilon=1.0,
    smoothness=0.5,
    min_area=10.0,
    corner_threshold=60.0,
)
svg_xml = vectorizer.vectorize("input.png")
```

## Local Interactive Web Studio

Launch local zero-dependency testing web app:

```bash
python3 server.py --port 8088
# or
./run_studio.sh
```

Open browser at `http://127.0.0.1:8088`.
- Drag & drop / paste (`⌘V`) any image.
- Click any of the 4 procedural instant test presets (Target Rings, Geometric Crest, Script Monogram, Chromatic Blocks).
- Live sliders for Colors, RDP $\epsilon$, Bézier Smoothness, Min Area cutoff, and Corner Threshold.
- Side-by-side view, draggable curtain split comparison, and raw SVG code inspector with 1-click download.

## Running Tests

```bash
python3 test_vectorizer.py
```
