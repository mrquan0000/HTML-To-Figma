#!/usr/bin/env python3
"""
Deterministic Figma builder — reads spec.json (v2) and constructs the corresponding
Figma frame via figma-mcp-go (stdio MCP).

Usage:
    python agents/figma_builder.py --spec output/scene_1_spec.json \
                                    --report output/scene_1_report.json

Requires Figma desktop app open with the figma-mcp-go plugin running.
Spawns its own MCP server process (becomes a FOLLOWER if another exists).

Build order (matches spec order, which is sorted by effective z-index):
  1. Create top-level frame at (0,0)
  2. For each element in spec.elements:
       create node → set properties → store id mapping
  3. Apply post-creation properties (fills/strokes/effects/rotation)

Atomicity: if any step fails, deletes the partially-built frame and re-raises.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue


# ═════════════════════════════════════════════════════════════════════════════
# Minimal MCP stdio client (JSON-RPC 2.0 over newline-delimited JSON)
# ═════════════════════════════════════════════════════════════════════════════

class MCPClient:
    """Bare-minimum MCP client. Synchronous request/response over stdio."""

    def __init__(self, command: list[str], log_stderr: bool = False):
        self.command = command
        self.log_stderr = log_stderr
        self.proc: subprocess.Popen | None = None
        self._next_id = 1
        self._responses: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._closing = False

    def __enter__(self):
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        if self.log_stderr:
            self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
            self._stderr_thread.start()
        else:
            # Drain stderr so the pipe doesn't fill and block the child
            self._stderr_thread = threading.Thread(target=self._stderr_drain, daemon=True)
            self._stderr_thread.start()
        # Handshake
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "figma-builder", "version": "2.0"},
        })
        self.notify("notifications/initialized", {})
        return self

    def __exit__(self, exc_type, exc, tb):
        self._closing = True
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    def _reader(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = obj.get("id")
            if rid is not None:
                with self._lock:
                    self._responses[rid] = obj

    def _stderr_drain(self):
        assert self.proc and self.proc.stderr
        for _ in self.proc.stderr:
            pass

    def _stderr_reader(self):
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            sys.stderr.write("[mcp] " + line)

    def _send(self, obj: dict):
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(f"MCP request {method} timed out after {timeout}s")

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 30.0) -> dict:
        """Call a tool by name. Returns the `result` dict. Raises on isError."""
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"MCP error calling {name}: {resp['error']}")
        result = resp.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            msg = content[0].get("text", "") if content else "(no message)"
            raise RuntimeError(f"Tool '{name}' failed: {msg}")
        return result


# ═════════════════════════════════════════════════════════════════════════════
# Color helpers
# ═════════════════════════════════════════════════════════════════════════════

def rgba_to_hex(c: dict | None) -> str | None:
    """Convert {r,g,b,a} 0..1 floats → '#RRGGBB' (alpha goes to opacity param)."""
    if not c:
        return None
    r = int(round(c.get("r", 0) * 255))
    g = int(round(c.get("g", 0) * 255))
    b = int(round(c.get("b", 0) * 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def rgba_to_hex8(c: dict | None) -> str | None:
    """Convert {r,g,b,a} → '#RRGGBBAA'. figma-mcp-go encodes paint alpha in the
    hex (get_node returns fills like '#2d1e0f26'); set_strokes has no opacity
    param, so a translucent stroke must carry its alpha in the hex itself."""
    if not c:
        return None
    a = int(round(c.get("a", 1.0) * 255))
    return f"{rgba_to_hex(c)}{a:02X}"


def first_solid_fill(fills: list[dict] | None) -> tuple[str | None, float]:
    """Return (hex, alpha) of first SOLID fill, else (None, 1.0)."""
    for f in fills or []:
        if f.get("type") == "SOLID":
            c = f.get("color") or {}
            return rgba_to_hex(c), c.get("a", 1.0)
    return None, 1.0


# Brand palette — derived from the colors used consistently across the source
# scene HTMLs. Multi-run text (which figma-mcp-go cannot style per-range) keeps
# one editable text node, and its single color is snapped to the nearest brand
# color here. EDIT THIS LIST to match your brand. Single-run text is untouched.
BRAND_PALETTE = [
    "#CF9A34", "#F6C71E", "#E8973F", "#D36708",   # gold / orange
    "#150A01", "#200F00", "#542A06",              # dark browns
    "#35CC23", "#C21403", "#A9ADB1",              # green / red / gray
    "#FFFFFF", "#000000",
]


def nearest_brand_color(hex_color: str | None) -> str | None:
    """Snap a hex color to the nearest BRAND_PALETTE entry by RGB distance."""
    if not hex_color:
        return hex_color
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    best, best_d = hex_color, None
    for cand in BRAND_PALETTE:
        c = cand.lstrip("#")
        cr, cg, cb = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_d is None or d < best_d:
            best, best_d = cand, d
    return best


def file_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


# ═════════════════════════════════════════════════════════════════════════════
# Node-ID extraction from tool response text
# ═════════════════════════════════════════════════════════════════════════════

_ID_PATTERNS = [
    re.compile(r"'(\d+:\d+)'"),                  # 'I:123' single-quoted
    re.compile(r'"(\d+:\d+)"'),                  # "I:123" double-quoted
    re.compile(r"\bid[:=]\s*(\d+:\d+)\b", re.I), # id: I:123
    re.compile(r"\b(\d+:\d+)\b"),                # bare I:123 (last resort)
]


def extract_node_id(result: dict) -> str | None:
    content = result.get("content") or []
    for item in content:
        text = item.get("text", "")
        for pat in _ID_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Spec → MCP translation
# ═════════════════════════════════════════════════════════════════════════════

class FigmaBuilder:
    def __init__(self, client: MCPClient, spec: dict, spec_dir: Path, verbose: bool = False):
        self.client = client
        self.spec = spec
        self.spec_dir = spec_dir
        self.verbose = verbose
        self.uid_to_node_id: dict[str, str] = {}
        self.warnings: list[str] = []
        self.frame_id: str | None = None
        # Absolute coords for each spec element (used to compute parent-relative
        # positions before sending to Figma, which interprets x/y as local to parent).
        self.uid_to_abs_xy: dict[str, tuple[int, int]] = {
            el["id"]: (el.get("x", 0), el.get("y", 0)) for el in spec.get("elements", [])
        }

    def _local_xy(self, el: dict) -> tuple[int, int]:
        """Convert absolute spec coords to parent-relative (Figma local coords)."""
        ax, ay = el.get("x", 0), el.get("y", 0)
        parent_uid = el.get("parent_id")
        if parent_uid and parent_uid in self.uid_to_abs_xy:
            px, py = self.uid_to_abs_xy[parent_uid]
            return ax - px, ay - py
        return ax, ay  # parent is main frame, abs == local

    # ─── Build ──────────────────────────────────────────────────────────────

    def build(self) -> dict:
        spec = self.spec
        frame_name = spec.get("frame_name", "Imported")
        frame_w = spec["frame_width"]
        frame_h = spec["frame_height"]
        frame_bg_hex = rgba_to_hex(spec.get("frame_bg")) or "#FFFFFF"

        # 1. Create top-level frame
        frame_args = {
            "name": frame_name,
            "width": frame_w,
            "height": frame_h,
            "x": self._pick_canvas_x(frame_w),
            "y": 0,
            "fillColor": frame_bg_hex,
        }
        self._log(f"create_frame {frame_args}")
        res = self.client.call_tool("create_frame", frame_args)
        frame_id = extract_node_id(res)
        if not frame_id:
            raise RuntimeError(f"create_frame returned no id: {res}")
        self.frame_id = frame_id
        self.uid_to_node_id["__frame__"] = frame_id

        # 2. Build elements in spec order (already sorted by z)
        try:
            for el in spec["elements"]:
                self._build_one(el)
        except Exception:
            # Cleanup on error
            try:
                self.client.call_tool("delete_nodes", {"nodeIds": [frame_id]})
            except Exception:
                pass
            raise

        return {
            "frame_id": frame_id,
            "element_count": len(spec["elements"]),
            "uid_to_node_id": self.uid_to_node_id,
            "warnings": self.warnings + spec.get("warnings", []),
        }

    def _pick_canvas_x(self, frame_w: int) -> int:
        """Avoid overlapping existing frames: place new frame to the right of the rightmost existing."""
        try:
            doc = self.client.call_tool("get_document", {})
            text = " ".join(item.get("text", "") for item in (doc.get("content") or []))
            xs = [int(m) for m in re.findall(r'"x":\s*(-?\d+)', text)]
            ws = [int(m) for m in re.findall(r'"width":\s*(-?\d+)', text)]
            if xs and ws:
                rightmost = max(x + w for x, w in zip(xs, ws[:len(xs)]))
                return rightmost + 200
        except Exception:
            pass
        return 0

    # ─── Per-element dispatch ───────────────────────────────────────────────

    def _build_one(self, el: dict):
        t = el["type"]
        parent_id = self._parent_node_id(el)
        if t == "rectangle":
            self._create_rectangle(el, parent_id)
        elif t == "ellipse":
            self._create_ellipse(el, parent_id)
        elif t == "frame":
            self._create_inner_frame(el, parent_id)
        elif t == "group":
            self.warnings.append(f"group '{el['name']}' not implemented (skipped)")
        elif t == "text":
            self._create_text(el, parent_id)
        elif t == "image":
            self._create_image(el, parent_id)
        else:
            self.warnings.append(f"unknown element type: {t}")

    def _parent_node_id(self, el: dict) -> str:
        parent_uid = el.get("parent_id")
        if parent_uid and parent_uid in self.uid_to_node_id:
            return self.uid_to_node_id[parent_uid]
        return self.frame_id  # type: ignore[return-value]

    # ─── Element creators ──────────────────────────────────────────────────

    def _create_rectangle(self, el: dict, parent_id: str):
        fill_hex, fill_alpha = first_solid_fill(el.get("fills"))
        lx, ly = self._local_xy(el)
        args = {
            "name": el["name"],
            "parentId": parent_id,
            "x": lx, "y": ly,
            "width": el["width"], "height": el["height"],
        }
        if fill_hex:
            args["fillColor"] = fill_hex
        # Uniform corner radius shortcut (otherwise apply set_corner_radius after)
        radii = el.get("corner_radii") or [0, 0, 0, 0]
        if len(set(radii)) == 1 and radii[0] > 0:
            args["cornerRadius"] = radii[0]
        res = self.client.call_tool("create_rectangle", args)
        node_id = extract_node_id(res)
        if not node_id:
            self.warnings.append(f"create_rectangle '{el['name']}': no id returned")
            return
        self.uid_to_node_id[el["id"]] = node_id
        # Figma shapes default to a gray/white fill. A shape with no design fill
        # (e.g. .neon-border: only an inset-shadow glow, no background) must be
        # transparent, else the default fill covers content. Effects/strokes are
        # applied separately by _apply_post_create and are unaffected.
        if not fill_hex:
            try:
                self.client.call_tool("set_fills", {"nodeId": node_id, "color": "#000000", "opacity": 0})
            except Exception as e:
                self.warnings.append(f"transparent fill '{el['name']}': {e}")
        self._apply_post_create(node_id, el, fill_alpha)
        # Non-uniform corner radii
        if len(set(radii)) > 1:
            self._set_corner_radii(node_id, radii)

    def _create_ellipse(self, el: dict, parent_id: str):
        fill_hex, fill_alpha = first_solid_fill(el.get("fills"))
        lx, ly = self._local_xy(el)
        args = {
            "name": el["name"],
            "parentId": parent_id,
            "x": lx, "y": ly,
            "width": el["width"], "height": el["height"],
        }
        if fill_hex:
            args["fillColor"] = fill_hex
        res = self.client.call_tool("create_ellipse", args)
        node_id = extract_node_id(res)
        if not node_id:
            self.warnings.append(f"create_ellipse '{el['name']}': no id returned")
            return
        self.uid_to_node_id[el["id"]] = node_id
        # Empty fill → transparent (avoid Figma's default gray/white). See _create_rectangle.
        if not fill_hex:
            try:
                self.client.call_tool("set_fills", {"nodeId": node_id, "color": "#000000", "opacity": 0})
            except Exception as e:
                self.warnings.append(f"transparent fill '{el['name']}': {e}")
        self._apply_post_create(node_id, el, fill_alpha)

    def _create_inner_frame(self, el: dict, parent_id: str):
        fill_hex, fill_alpha = first_solid_fill(el.get("fills"))
        lx, ly = self._local_xy(el)
        args = {
            "name": el["name"],
            "parentId": parent_id,
            "x": lx, "y": ly,
            "width": el["width"], "height": el["height"],
            "layoutMode": "NONE",
        }
        if fill_hex:
            args["fillColor"] = fill_hex
        res = self.client.call_tool("create_frame", args)
        node_id = extract_node_id(res)
        if not node_id:
            self.warnings.append(f"create_frame '{el['name']}': no id returned")
            return
        self.uid_to_node_id[el["id"]] = node_id
        # Figma frames default to an opaque WHITE fill. A frame with no design
        # background (empty fills — e.g. a pure clip container, or a gradient
        # container whose visual lives in its BG-Gradient PNG) must be made
        # transparent, else the white leaks through (e.g. the transparent corners
        # of a rotated gradient PNG). The master/scene frame uses frame_bg and is
        # created elsewhere, so it is unaffected.
        if not fill_hex:
            try:
                self.client.call_tool("set_fills", {"nodeId": node_id, "color": "#000000", "opacity": 0})
            except Exception as e:
                self.warnings.append(f"transparent fill '{el['name']}': {e}")
        self._apply_post_create(node_id, el, fill_alpha)
        radii = el.get("corner_radii") or [0, 0, 0, 0]
        if any(r > 0 for r in radii):
            self._set_corner_radii(node_id, radii)

    def _create_text(self, el: dict, parent_id: str):
        runs = el.get("runs") or []
        # Concatenate runs (figma-mcp-go has no per-range styling tool — fidelity limitation)
        full_text = "".join(r.get("text", "") for r in runs).strip()
        if not full_text:
            return
        # Choose the style source run. figma-mcp-go has no per-range text styling,
        # so a multi-run text must collapse to ONE style. For multi-run text we
        # pick the DOMINANT run (most non-whitespace chars) — the body color —
        # rather than just the first run, and snap its color to the brand palette.
        # Single-run text keeps its exact style/color. Missing per-word highlights
        # are re-added by the user in After Effects.
        nonempty = [r for r in runs if r.get("text", "").strip()]
        is_multi_run = len(nonempty) > 1
        if is_multi_run:
            style_run = max(nonempty, key=lambda r: len(r.get("text", "").strip()))
        else:
            style_run = nonempty[0] if nonempty else (runs[0] if runs else {})
        font_family = style_run.get("font_family") or "Inter"
        font_size = style_run.get("font_size") or 14
        font_weight = style_run.get("font_weight") or 400
        italic = style_run.get("italic", False)

        # Text color: first SOLID fill of style run; snap to brand for multi-run.
        fill_hex, fill_alpha = first_solid_fill(style_run.get("fills"))
        if is_multi_run:
            fill_hex = nearest_brand_color(fill_hex)
        lx, ly = self._local_xy(el)
        base_args = {
            "name": el["name"],
            "parentId": parent_id,
            "x": lx, "y": ly,
            "text": full_text,
            "fontSize": float(font_size),
        }
        if fill_hex:
            base_args["fillColor"] = fill_hex

        # Try (font_family, style) combos until one works. Final fallback: Inter Regular.
        families = [font_family, "Inter"] if font_family != "Inter" else ["Inter"]
        styles = self._font_style_candidates(font_weight, italic)
        res = None
        last_err = None
        used_family, used_style = None, None
        for fam in families:
            for sty in styles:
                try:
                    args = {**base_args, "fontFamily": fam, "fontStyle": sty}
                    res = self.client.call_tool("create_text", args)
                    used_family, used_style = fam, sty
                    break
                except RuntimeError as e:
                    last_err = e
                    if "could not be loaded" not in str(e) and "font" not in str(e).lower():
                        raise
            if res is not None:
                break
        if res is None:
            raise RuntimeError(f"create_text '{el['name']}': no font worked. Last: {last_err}")
        if used_family != font_family or used_style != self._font_style_candidates(font_weight, italic)[0]:
            self.warnings.append(
                f"text '{full_text[:30]}': font fallback {font_family}/{font_weight} → {used_family}/{used_style}"
            )
        node_id = extract_node_id(res)
        if not node_id:
            self.warnings.append(f"create_text '{el['name']}': no id returned")
            return
        self.uid_to_node_id[el["id"]] = node_id
        # Figma text defaults to an opaque fill. A run with no design fill (e.g.
        # rim-only text with CSS color:transparent, glyph body invisible, only
        # -webkit-text-stroke shows) must be made transparent, else the default
        # paints a solid glyph over the stroke/underlying layers. See _create_rectangle.
        if not fill_hex:
            try:
                self.client.call_tool("set_fills", {"nodeId": node_id, "color": "#000000", "opacity": 0})
            except Exception as e:
                self.warnings.append(f"transparent fill '{el['name']}': {e}")
        # Resize only multi-line/wrapping paragraphs to their laid-out box.
        # Single-line text keeps Figma's auto-width so it never wraps/clips when
        # the platform font renders slightly wider than Chromium measured.
        lh = el.get("line_height") or float(font_size) * 1.2
        is_multiline = ("\n" in full_text) or (el["height"] > lh * 1.4)
        if is_multiline:
            try:
                self.client.call_tool("resize_nodes", {
                    "nodeIds": [node_id], "width": el["width"], "height": el["height"],
                })
            except Exception as e:
                self.warnings.append(f"resize_nodes text {el['name']}: {e}")
        # Opacity, rotation
        self._apply_opacity_rotation(node_id, el, fill_alpha)
        # Glow / soft shadow from CSS filter:drop-shadow → native effect on the text
        self._apply_effects(node_id, el.get("effects") or [])
        # -webkit-text-stroke → glyph outline on the text node (kept editable)
        strokes = el.get("strokes") or []
        if strokes and strokes[0].get("type") == "SOLID":
            try:
                self.client.call_tool("set_strokes", {
                    "nodeId": node_id,
                    "color": rgba_to_hex(strokes[0].get("color")),
                    "strokeWeight": el.get("stroke_weight", 1) or 1,
                })
            except Exception as e:
                self.warnings.append(f"set_strokes text {el['name']}: {e}")
        # Multi-run flag
        if is_multi_run:
            self.warnings.append(
                f"text '{el['name'][:30]}' has multiple style runs; kept editable, "
                f"color snapped to brand {fill_hex} (per-word highlight to be added in AE)"
            )

    def _create_image(self, el: dict, parent_id: str):
        img_rel = el.get("image_path")
        if not img_rel:
            self.warnings.append(f"image '{el['name']}': missing image_path")
            return
        # image_path is relative to project CWD (extractor stored it that way).
        # Fallback: also try relative to spec.json location.
        candidates = [Path(img_rel), self.spec_dir / img_rel, self.spec_dir.parent / img_rel]
        img_abs = next((p.resolve() for p in candidates if p.exists()), None)
        if not img_abs:
            self.warnings.append(f"image '{el['name']}': file missing: {img_rel}")
            return
        b64 = file_to_base64(str(img_abs))
        lx, ly = self._local_xy(el)
        args = {
            "name": el["name"],
            "parentId": parent_id,
            "x": lx, "y": ly,
            "width": el["width"], "height": el["height"],
            "imageData": b64,
            "scaleMode": "FILL",
        }
        res = self.client.call_tool("import_image", args)
        node_id = extract_node_id(res)
        if not node_id:
            self.warnings.append(f"import_image '{el['name']}': no id returned")
            return
        self.uid_to_node_id[el["id"]] = node_id
        # Opacity + rotation (no fill colors applicable to image — bg is the PNG)
        self._apply_opacity_rotation(node_id, el, 1.0)
        # Effects can still apply to image nodes (drop shadow etc.)
        self._apply_effects(node_id, el.get("effects") or [])

    # ─── Post-creation property application ─────────────────────────────────

    def _apply_post_create(self, node_id: str, el: dict, fill_alpha: float):
        # Stroke (only single-color stroke supported — uniform border case)
        strokes = el.get("strokes") or []
        if strokes:
            s = strokes[0]
            if s.get("type") == "SOLID":
                # 8-digit hex carries the stroke alpha (set_strokes has no opacity
                # param). A translucent border — e.g. rgba(246,199,30,0.2) glass rim
                # — must NOT render as opaque bright gold.
                hex_color = rgba_to_hex8(s.get("color"))
                weight = el.get("stroke_weight", 1) or 1
                try:
                    self.client.call_tool("set_strokes", {
                        "nodeId": node_id,
                        "color": hex_color,
                        "strokeWeight": weight,
                    })
                except Exception as e:
                    self.warnings.append(f"set_strokes {node_id}: {e}")

        # Fill alpha (figma-mcp-go set_fills takes opacity; create_* already set the color)
        if fill_alpha < 0.999:
            try:
                self.client.call_tool("set_fills", {
                    "nodeId": node_id,
                    "color": rgba_to_hex((el.get("fills") or [{}])[0].get("color")) or "#000000",
                    "opacity": round(fill_alpha, 3),
                })
            except Exception as e:
                self.warnings.append(f"set_fills(alpha) {node_id}: {e}")

        self._apply_effects(node_id, el.get("effects") or [])
        self._apply_opacity_rotation(node_id, el, fill_alpha)

    def _apply_effects(self, node_id: str, effects: list[dict]):
        if not effects:
            return
        # figma-mcp-go expects array of effect objects with figma-style fields
        figma_effects = []
        for e in effects:
            t = e.get("type")
            if t in ("DROP_SHADOW", "INNER_SHADOW"):
                c = e.get("color") or {"r": 0, "g": 0, "b": 0, "a": 0.25}
                off = e.get("offset", {"x": 0, "y": 4})
                figma_effects.append({
                    "type": t,
                    "color": rgba_to_hex(c) or "#000000",
                    "opacity": round(c.get("a", 1), 3),
                    "offsetX": off.get("x", 0),
                    "offsetY": off.get("y", 4),
                    "radius": e.get("radius", 8),
                    "spread": e.get("spread", 0),
                    "visible": True,
                })
            elif t in ("LAYER_BLUR", "BACKGROUND_BLUR"):
                figma_effects.append({
                    "type": t,
                    "radius": e.get("radius", 4),
                    "visible": True,
                })
        if not figma_effects:
            return
        try:
            self.client.call_tool("set_effects", {
                "nodeId": node_id,
                "effects": figma_effects,
            })
        except Exception as e:
            self.warnings.append(f"set_effects {node_id}: {e}")

    def _apply_opacity_rotation(self, node_id: str, el: dict, fill_alpha: float):
        opacity = el.get("opacity", 1.0)
        if opacity < 0.999:
            try:
                self.client.call_tool("set_opacity", {
                    "nodeIds": [node_id], "opacity": round(opacity, 3),
                })
            except Exception as e:
                self.warnings.append(f"set_opacity {node_id}: {e}")
        rotation = el.get("rotation", 0) or 0
        if abs(rotation) > 0.1:
            try:
                self.client.call_tool("rotate_nodes", {
                    "nodeIds": [node_id], "rotation": rotation,
                })
            except Exception as e:
                self.warnings.append(f"rotate_nodes {node_id}: {e}")

    def _set_corner_radii(self, node_id: str, radii: list[int]):
        tl, tr, br, bl = radii
        try:
            self.client.call_tool("set_corner_radius", {
                "nodeIds": [node_id],
                "topLeftRadius": tl,
                "topRightRadius": tr,
                "bottomRightRadius": br,
                "bottomLeftRadius": bl,
            })
        except Exception as e:
            self.warnings.append(f"set_corner_radius {node_id}: {e}")

    @staticmethod
    def _font_style_candidates(weight: int, italic: bool) -> list[str]:
        """Return ordered candidate style names — figma-mcp-go's accepted form varies per font.
        Tries no-space form first ('SemiBold'), then spaced ('Semi Bold'), then 'Regular' fallback.
        """
        base = {
            100: ("Thin",),
            200: ("ExtraLight", "Extra Light"),
            300: ("Light",),
            400: ("Regular",),
            500: ("Medium",),
            600: ("SemiBold", "Semi Bold", "Semibold"),
            700: ("Bold",),
            800: ("ExtraBold", "Extra Bold"),
            900: ("Black", "Heavy"),
        }
        closest = min(base.keys(), key=lambda k: abs(k - weight))
        names = list(base[closest])
        if italic:
            italic_variants = []
            for n in names:
                italic_variants.append(n + " Italic")
                italic_variants.append(n + "Italic")
            italic_variants.append("Italic")
            names = italic_variants + names  # try italic forms first
        # Always include Regular as final fallback
        if "Regular" not in names:
            names.append("Regular")
        return names

    def _log(self, msg: str):
        if self.verbose:
            print(f"[builder] {msg}", file=sys.stderr)


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Build Figma frame from spec.json v2")
    ap.add_argument("--spec", required=True, help="Path to spec.json")
    ap.add_argument("--report", help="Path to write report.json (default: <spec>_report.json)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--mcp-cmd", default="npx",
                    help="MCP server command (default: npx)")
    ap.add_argument("--mcp-args", default="-y,@vkhanhqui/figma-mcp-go@latest",
                    help="Comma-separated args for MCP command")
    args = ap.parse_args()

    spec_path = Path(args.spec).resolve()
    spec_dir = spec_path.parent
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("version") != 2:
        print(f"WARN: spec version {spec.get('version')} != 2", file=sys.stderr)

    report_path = Path(args.report) if args.report else spec_path.with_name(spec_path.stem + "_report.json")
    cmd = [args.mcp_cmd, *args.mcp_args.split(",")]

    print(f"[builder] spawning MCP: {' '.join(cmd)}", file=sys.stderr)
    with MCPClient(cmd, log_stderr=args.verbose) as client:
        builder = FigmaBuilder(client, spec, spec_dir, verbose=args.verbose)
        try:
            report = builder.build()
            status = "ok"
        except Exception as e:
            report = {
                "error": str(e),
                "frame_id": builder.frame_id,
                "uid_to_node_id": builder.uid_to_node_id,
                "warnings": builder.warnings,
            }
            status = "error"
            print(f"[builder] BUILD FAILED: {e}", file=sys.stderr)

    report["status"] = status
    report["spec"] = str(spec_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[builder] report → {report_path} ({status})", file=sys.stderr)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
