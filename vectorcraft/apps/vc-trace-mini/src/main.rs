//! vc-trace-mini: Lightweight standalone vectorizer CLI extracted from VectorCraft.
//! Built specifically for apparel CAD plugins (Browzwear VStitcher, CLO3D, Gerber).

#![forbid(unsafe_code)]

use std::fmt::Write as _;
use std::fs;
use std::io::{self, Read};
use std::path::Path;
use std::process::ExitCode;
use std::time::Instant;

use kurbo::{BezPath, PathEl};
use serde_json::{Value, json};
use vectorcraft_trace::{
    Method, Mode, PRESET_NAMES, Raster, TraceParams, TraceResult, preset, trace,
};

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn print_help() {
    println!(
        r#"vc-trace-mini {VERSION} - Ultra-lightweight Vectorizer CLI (VectorCraft Kernel)

USAGE:
    vc-trace-mini [OPTIONS] <INPUT_IMAGE>
    vc-trace-mini -i <INPUT_IMAGE> -o <OUTPUT_SVG> [OPTIONS]
    cat <IMAGE> | vc-trace-mini [OPTIONS] > <OUTPUT_SVG>

ARGUMENTS:
    <INPUT_IMAGE>                  Path to raster image (PNG, JPEG, WebP, GIF, BMP)

OPTIONS:
    -i, --in <PATH>                Input image file path
    -o, --out <PATH>               Output SVG file path (default: stdout or <input>.svg)
    -p, --preset <NAME>            Trace preset (default: "Flat Logo")
                                   Options: Flat Logo, Black and White Logo, Silhouettes,
                                   Line Art, Technical Drawing, Sketched Art, 3 Colors,
                                   6 Colors, 16 Colors, Shades of Gray, Auto Color,
                                   High Fidelity Photo, Low Fidelity Photo, Outline, Default
        --presets                  List all built-in presets and exit

PARAMETER OVERRIDES:
        --mode <MODE>              bw | gray | color | logo
        --colors <N>               Number of palette colors (2..256)
        --threshold <0..255>       B&W binarization threshold (default: 128)
        --noise <N>                Minimum region size in pixels to keep (default: 25)
        --paths <0..100>           Path curve fidelity (higher = tighter to pixels)
        --corners <0..100>         Corner sharpness fidelity (higher = sharper corners)
        --ignore-white             Omit white background regions (default for logos)
        --no-ignore-white          Preserve white background shapes
        --ignore-color <#HEX>      Omit custom background color (e.g. '#ffffff')
        --overlapping              Trace stacked overlapping layers (prevents hairline gaps)
        --abutting                 Trace abutting flush layers (default)
        --shapes                   Recognize true geometric shapes (circles, rects, polygons)
        --strokes                  Trace thin lines as stroked centerlines
        --stroke-width <PX>        Maximum stroke width for centerline tracing
        --snap                     Snap near-straight curves to lines & arcs to circles
        --gradients                Trace smooth color ramps as linear gradients
        --params <JSON>            Raw JSON parameter overrides

OUTPUT & DIAGNOSTICS:
        --json                     Output JSON summary of paths, anchors, timing and palette
    -h, --help                     Print help information
    -V, --version                  Print version information
"#
    );
}

fn normalize_preset_name(raw: &str) -> String {
    let lower = raw.trim().to_ascii_lowercase().replace(['-', '_'], " ");
    match lower.as_str() {
        "flat" | "flat logo" | "logo" => "Flat Logo".into(),
        "bw" | "b&w" | "black and white" | "black and white logo" => "Black and White Logo".into(),
        "silhouettes" | "silhouette" => "Silhouettes".into(),
        "sketched art" | "sketch" => "Sketched Art".into(),
        "line art" | "line" => "Line Art".into(),
        "technical drawing" | "technical" => "Technical Drawing".into(),
        "auto" | "auto color" => "Auto Color".into(),
        "high fidelity photo" | "high photo" | "photo" => "High Fidelity Photo".into(),
        "low fidelity photo" | "low photo" => "Low Fidelity Photo".into(),
        "3 colors" | "3" => "3 Colors".into(),
        "6 colors" | "6" => "6 Colors".into(),
        "16 colors" | "16" => "16 Colors".into(),
        "shades of gray" | "gray" | "grayscale" => "Shades of Gray".into(),
        "outline" => "Outline".into(),
        "default" => "Default".into(),
        _ => raw.trim().into(),
    }
}

fn bezpath_to_svg_d(bp: &BezPath) -> String {
    let mut d = String::new();
    for el in bp.elements() {
        match el {
            PathEl::MoveTo(p) => {
                let _ = write!(d, "M{:.3} {:.3} ", p.x, p.y);
            }
            PathEl::LineTo(p) => {
                let _ = write!(d, "L{:.3} {:.3} ", p.x, p.y);
            }
            PathEl::QuadTo(p1, p2) => {
                let _ = write!(d, "Q{:.3} {:.3} {:.3} {:.3} ", p1.x, p1.y, p2.x, p2.y);
            }
            PathEl::CurveTo(p1, p2, p3) => {
                let _ = write!(
                    d,
                    "C{:.3} {:.3} {:.3} {:.3} {:.3} {:.3} ",
                    p1.x, p1.y, p2.x, p2.y, p3.x, p3.y
                );
            }
            PathEl::ClosePath => {
                d.push_str("Z ");
            }
        }
    }
    if d.ends_with(' ') {
        d.pop();
    }
    d
}

fn render_svg(result: &TraceResult, width: u32, height: u32) -> String {
    let mut svg = String::with_capacity(32 * 1024);
    let _ = writeln!(
        svg,
        r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">"#
    );

    // Defs for linear gradients if generated
    let has_gradients = result.paths.iter().any(|p| p.gradient.is_some());
    if has_gradients {
        svg.push_str("  <defs>\n");
        for (i, p) in result.paths.iter().enumerate() {
            if let Some(grad) = &p.gradient {
                let _ = writeln!(
                    svg,
                    r#"    <linearGradient id="g{i}" x1="{:.2}" y1="{:.2}" x2="{:.2}" y2="{:.2}" gradientUnits="userSpaceOnUse">"#,
                    grad.start.x, grad.start.y, grad.end.x, grad.end.y
                );
                for (offset, color) in &grad.stops {
                    let _ = writeln!(
                        svg,
                        r#"      <stop offset="{:.1}%" stop-color="rgb({},{},{})" />"#,
                        offset * 100.0,
                        color[0],
                        color[1],
                        color[2]
                    );
                }
                svg.push_str("    </linearGradient>\n");
            }
        }
        svg.push_str("  </defs>\n");
    }

    for (i, p) in result.paths.iter().enumerate() {
        let bp = p.path.to_bezpath();
        let d = bezpath_to_svg_d(&bp);
        if d.is_empty() {
            continue;
        }

        if let Some(stroke_w) = p.stroke {
            let cap = if p.square_caps { "square" } else { "round" };
            let _ = writeln!(
                svg,
                r#"  <path d="{d}" fill="none" stroke="rgb({},{},{})" stroke-width="{stroke_w:.2}" stroke-linecap="{cap}" stroke-linejoin="round" />"#,
                p.color[0], p.color[1], p.color[2]
            );
        } else if p.gradient.is_some() {
            let _ = writeln!(
                svg,
                r#"  <path d="{d}" fill="url(#g{i})" fill-rule="evenodd" />"#
            );
        } else {
            let _ = writeln!(
                svg,
                r#"  <path d="{d}" fill="rgb({},{},{})" fill-rule="evenodd" />"#,
                p.color[0], p.color[1], p.color[2]
            );
        }
    }

    svg.push_str("</svg>\n");
    svg
}

fn apply_json_overrides(params: &mut TraceParams, patch: &Value) {
    if let Some(obj) = patch.as_object() {
        if let Some(v) = obj.get("mode").and_then(|v| v.as_str()) {
            match v.to_ascii_lowercase().as_str() {
                "bw" | "blackandwhite" => params.mode = Mode::BlackAndWhite,
                "gray" | "grayscale" => params.mode = Mode::Grayscale,
                "color" => params.mode = Mode::Color,
                "logo" | "flat" => params.mode = Mode::Logo,
                _ => {}
            }
        }
        if let Some(v) = obj.get("threshold").and_then(|v| v.as_u64()) {
            params.threshold = v.clamp(0, 255) as u8;
        }
        if let Some(v) = obj.get("colors").and_then(|v| v.as_u64()) {
            params.colors = v.clamp(2, 256) as u32;
        }
        if let Some(v) = obj.get("noise").and_then(|v| v.as_u64()) {
            params.noise = v as u32;
        }
        if let Some(v) = obj.get("paths").and_then(|v| v.as_f64()) {
            params.paths = v.clamp(0.0, 100.0);
        }
        if let Some(v) = obj.get("corners").and_then(|v| v.as_f64()) {
            params.corners = v.clamp(0.0, 100.0);
        }
        if let Some(v) = obj.get("ignoreWhite").and_then(|v| v.as_bool()) {
            params.ignore_white = v;
        }
        if let Some(v) = obj.get("ignoreColor").and_then(|v| v.as_str()) {
            params.ignore_color = Some(v.to_string());
        }
        if let Some(v) = obj.get("shapes").and_then(|v| v.as_bool()) {
            params.shapes = v;
        }
        if let Some(v) = obj.get("strokes").and_then(|v| v.as_bool()) {
            params.strokes = v;
        }
        if let Some(v) = obj.get("strokeWidth").and_then(|v| v.as_f64()) {
            params.stroke_width = v;
        }
        if let Some(v) = obj.get("snapCurvesToLines").and_then(|v| v.as_bool()) {
            params.snap_curves_to_lines = v;
        }
        if let Some(v) = obj.get("gradients").and_then(|v| v.as_bool()) {
            params.gradients = v;
        }
        if let Some(v) = obj.get("method").and_then(|v| v.as_str()) {
            match v.to_ascii_lowercase().as_str() {
                "overlapping" => params.method = Method::Overlapping,
                "abutting" => params.method = Method::Abutting,
                _ => {}
            }
        }
    }
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        print_help();
        return Ok(());
    }

    let mut input_path: Option<String> = None;
    let mut output_path: Option<String> = None;
    let mut preset_name = "Flat Logo".to_string();
    let mut mode_override: Option<String> = None;
    let mut colors_override: Option<u32> = None;
    let mut threshold_override: Option<u8> = None;
    let mut noise_override: Option<u32> = None;
    let mut paths_override: Option<f64> = None;
    let mut corners_override: Option<f64> = None;
    let mut ignore_white_override: Option<bool> = None;
    let mut ignore_color_override: Option<String> = None;
    let mut method_override: Option<Method> = None;
    let mut shapes_override: Option<bool> = None;
    let mut strokes_override: Option<bool> = None;
    let mut stroke_width_override: Option<f64> = None;
    let mut snap_override: Option<bool> = None;
    let mut gradients_override: Option<bool> = None;
    let mut json_params: Option<String> = None;
    let mut output_json = false;

    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_help();
                return Ok(());
            }
            "-V" | "--version" => {
                println!("vc-trace-mini {VERSION}");
                return Ok(());
            }
            "--presets" => {
                println!("Available VectorCraft Trace Presets:");
                for p in PRESET_NAMES {
                    println!("  - {p}");
                }
                return Ok(());
            }
            "-i" | "--in" => {
                input_path = it.next().cloned();
            }
            "-o" | "--out" => {
                output_path = it.next().cloned();
            }
            "-p" | "--preset" => {
                if let Some(p) = it.next() {
                    preset_name = normalize_preset_name(p);
                }
            }
            "--mode" => {
                mode_override = it.next().cloned();
            }
            "--colors" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    colors_override = Some(v);
                }
            }
            "--threshold" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    threshold_override = Some(v);
                }
            }
            "--noise" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    noise_override = Some(v);
                }
            }
            "--paths" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    paths_override = Some(v);
                }
            }
            "--corners" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    corners_override = Some(v);
                }
            }
            "--ignore-white" => {
                ignore_white_override = Some(true);
            }
            "--no-ignore-white" => {
                ignore_white_override = Some(false);
            }
            "--ignore-color" => {
                ignore_color_override = it.next().cloned();
            }
            "--overlapping" => {
                method_override = Some(Method::Overlapping);
            }
            "--abutting" => {
                method_override = Some(Method::Abutting);
            }
            "--shapes" => {
                shapes_override = Some(true);
            }
            "--strokes" => {
                strokes_override = Some(true);
            }
            "--stroke-width" => {
                if let Some(v) = it.next().and_then(|s| s.parse().ok()) {
                    stroke_width_override = Some(v);
                }
            }
            "--snap" => {
                snap_override = Some(true);
            }
            "--gradients" => {
                gradients_override = Some(true);
            }
            "--params" => {
                json_params = it.next().cloned();
            }
            "--json" => {
                output_json = true;
            }
            non_flag if !non_flag.starts_with('-') => {
                if input_path.is_none() {
                    input_path = Some(non_flag.to_string());
                } else if output_path.is_none() {
                    output_path = Some(non_flag.to_string());
                }
            }
            unknown => {
                return Err(format!("Unknown option: {unknown}. Run with --help for options."));
            }
        }
    }

    // Read input bytes
    let t0 = Instant::now();
    let (bytes, auto_output_path) = match &input_path {
        Some(p) => {
            let buf = fs::read(p).map_err(|e| format!("Failed to read input file '{p}': {e}"))?;
            let auto_out = Path::new(p).with_extension("svg").to_string_lossy().to_string();
            (buf, Some(auto_out))
        }
        None => {
            // Read from stdin
            let mut buf = Vec::new();
            io::stdin()
                .read_to_end(&mut buf)
                .map_err(|e| format!("Failed to read stdin: {e}"))?;
            if buf.is_empty() {
                return Err("No input provided via file or stdin.".into());
            }
            (buf, None)
        }
    };

    // Decode raster
    let raster = Raster::decode_to_fit(&bytes)
        .map_err(|e| format!("Failed to decode image: {e}"))?;
    let decode_ms = t0.elapsed().as_secs_f64() * 1000.0;

    // Resolve trace params
    let mut params = preset(&preset_name)
        .or_else(|| preset("Flat Logo"))
        .unwrap_or_default();

    if let Some(m) = mode_override {
        match m.to_ascii_lowercase().as_str() {
            "bw" | "blackandwhite" => params.mode = Mode::BlackAndWhite,
            "gray" | "grayscale" => params.mode = Mode::Grayscale,
            "color" => params.mode = Mode::Color,
            "logo" | "flat" => params.mode = Mode::Logo,
            _ => {}
        }
    }
    if let Some(c) = colors_override {
        params.colors = c;
    }
    if let Some(t) = threshold_override {
        params.threshold = t;
    }
    if let Some(n) = noise_override {
        params.noise = n;
    }
    if let Some(p) = paths_override {
        params.paths = p;
    }
    if let Some(c) = corners_override {
        params.corners = c;
    }
    if let Some(w) = ignore_white_override {
        params.ignore_white = w;
    }
    if let Some(c) = ignore_color_override {
        params.ignore_color = Some(c);
    }
    if let Some(m) = method_override {
        params.method = m;
    }
    if let Some(s) = shapes_override {
        params.shapes = s;
    }
    if let Some(s) = strokes_override {
        params.strokes = s;
    }
    if let Some(w) = stroke_width_override {
        params.stroke_width = w;
    }
    if let Some(s) = snap_override {
        params.snap_curves_to_lines = s;
    }
    if let Some(g) = gradients_override {
        params.gradients = g;
    }

    if let Some(jp) = json_params {
        if let Ok(v) = serde_json::from_str::<Value>(&jp) {
            apply_json_overrides(&mut params, &v);
        }
    }

    // Execute trace
    let t_trace = Instant::now();
    let result = trace(&raster, &params);
    let trace_ms = t_trace.elapsed().as_secs_f64() * 1000.0;

    // Render SVG
    let svg = render_svg(&result, raster.width, raster.height);
    let total_ms = t0.elapsed().as_secs_f64() * 1000.0;

    // Palette hex codes
    let palette_hex: Vec<String> = result
        .palette
        .iter()
        .map(|c| format!("#{:02x}{:02x}{:02x}", c[0], c[1], c[2]))
        .collect();

    let target_out = output_path.or(auto_output_path);

    if output_json {
        let meta = json!({
            "status": "success",
            "width": raster.width,
            "height": raster.height,
            "pathCount": result.paths.len(),
            "anchorCount": result.anchor_count(),
            "palette": palette_hex,
            "preset": preset_name,
            "timing": {
                "decodeMs": decode_ms,
                "traceMs": trace_ms,
                "totalMs": total_ms
            },
            "outputFile": target_out,
            "svgLength": svg.len()
        });
        println!("{}", serde_json::to_string_pretty(&meta).unwrap());
    }

    // Save SVG file or print to stdout
    if let Some(ref out_file) = target_out {
        fs::write(out_file, &svg)
            .map_err(|e| format!("Failed to write SVG to '{out_file}': {e}"))?;
        if !output_json {
            eprintln!(
                "✓ Vectorized in {:.1}ms ({} paths, {} anchors) -> {}",
                total_ms,
                result.paths.len(),
                result.anchor_count(),
                out_file
            );
        }
    } else {
        print!("{svg}");
    }

    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::FAILURE
        }
    }
}
