# HTML-To-Figma — Claude Instructions (v2)

## Mục đích
Nhận HTML → render trong Chromium headless → extract layout/styles → emit **spec.json v2** → **figma_builder.py** gọi figma-mcp-go dựng frame trên Figma.

Pixel-perfect cho phần Figma vẽ được natively; raster PNG fallback cho phần phức tạp (SVG icons, gradients, filters, clip-path). Mục tiêu fidelity: ~95%+.

## Cách dùng

Khi user cung cấp đường dẫn HTML, ví dụ:
- `create figma from /path/to/file.html`
- `tạo figma từ input/scene_1.html`
- `html to figma ~/Downloads/page.html`

→ Chạy 2 lệnh dưới đây tuần tự, KHÔNG dừng, KHÔNG hỏi lại. Báo cáo 1 lần khi xong.

---

## Pipeline v2 — 2 bước

### Bước 1: Extract HTML → spec.json

```bash
.venv/bin/python agents/html_extractor.py \
    --input /PATH/TO/FILE.html \
    --output output/<scene_name>_spec.json
```

Tham số tùy chọn:
- `--viewport-width 600` (default 600) — nếu HTML dùng viewport khác (ví dụ 1200px), pass tham số phù hợp
- `--assets-dir output/assets/<scene_name>` — nơi lưu PNG fallback

**Output:**
- `output/<scene_name>_spec.json` — spec v2
- `output/assets/<scene_name>/*.png` — raster fallback cho SVG + gradient backgrounds + filter

### Bước 2: Build Figma frame từ spec

```bash
.venv/bin/python agents/figma_builder.py \
    --spec output/<scene_name>_spec.json \
    --report output/<scene_name>_report.json
```

**Yêu cầu:** Figma desktop app phải đang mở **plugin figma-mcp-go** (không phải web Figma).

Builder:
1. Spawn 1 instance figma-mcp-go (FOLLOWER role nếu đã có instance khác chạy)
2. JSON-RPC qua stdio
3. Tạo frame chính, place ở `x = max_existing_x + 200, y = 0`
4. Build từng element theo thứ tự z-index sắp xếp sẵn
5. Atomic: nếu lỗi giữa chừng → delete frame chính, rollback
6. Sinh `<scene_name>_report.json` với `frame_id`, mapping uid→node_id, warnings

### Bước 3: Visual validation (1 lần, không loop)

Sau khi builder báo `status: "ok"`:

1. Dùng `mcp__figma-mcp-go__get_screenshot` với `nodeIds=[<frame_id>]` để export PNG
2. So sánh với HTML gốc (mở bằng Read tool)
3. Check 5 điểm:
   - [ ] Background bg color + gradient bg PNGs đúng
   - [ ] Vị trí + size từng layer khớp
   - [ ] SVG icons (raster) hiển thị đúng pixel
   - [ ] Text content + màu + alignment đúng
   - [ ] Effects (shadow, blur) visible
4. **KHÔNG bao giờ tự sửa lỗi.** Nếu phát hiện bất kỳ lỗi nào khi kiểm tra chất lượng:
   - **DỪNG.** Báo cáo rõ tình trạng từng lỗi (lỗi gì, ở layer/node nào, sai so với HTML mẫu ra sao).
   - **HỎI user**: "Bạn có muốn tôi fix (các) lỗi này không?" trước khi đụng vào bất cứ thứ gì.
   - Chỉ tiến hành fix SAU KHI user đồng ý. Tuyệt đối KHÔNG tự ý sửa node Figma, KHÔNG tự sửa `html_extractor.py`/`figma_builder.py`, KHÔNG loop tự-fix.
   - Lý do: việc tự fix trước đây đã làm hỏng màu/layer không khớp HTML (vd scene_7, scene_8). User muốn toàn quyền quyết định fix gì.

### Bước 4: Report final

```
Đã tạo xong:
- Frame: <tên> (<width>×<height>px) — node id <id>
- Native layers: X (rectangle/ellipse/text/frame)
- Raster images: Y (SVG icons + gradient backgrounds + filter elements)
- Warnings: <list từ report.json>
- Validation: [PASS / list issues]
```

---

## Spec v2 schema (tóm tắt)

```json
{
  "version": 2,
  "frame_name": "scene_1",
  "frame_width": 690,
  "frame_height": 900,
  "frame_bg": {"r":0.08,"g":0.04,"b":0.0,"a":1.0},
  "assets_dir": "output/assets/scene_1",
  "warnings": [...],
  "elements": [
    {
      "id": "e8",
      "parent_id": "e7",
      "type": "rectangle" | "ellipse" | "text" | "image" | "frame" | "group",
      "name": "[card-glass/frame]",
      "x": 340, "y": 450, "width": 260, "height": 246,
      "rotation": 0, "opacity": 1.0, "z": 100013,

      // shape-only:
      "fills": [{"type":"SOLID","color":{r,g,b,a}}],
      "strokes": [{...}],
      "stroke_weight": 1,
      "corner_radii": [tl,tr,br,bl],
      "effects": [{"type":"DROP_SHADOW",...}],
      "clip_content": true,

      // text-only:
      "runs": [{"text":"...","font_family":"Montserrat","font_size":12,"font_weight":600,"fills":[...]}],
      "text_align": "CENTER",
      "line_height": 18,

      // image-only:
      "image_path": "output/assets/scene_1/e8.png"
    }
  ]
}
```

Elements **đã sắp xếp sẵn** theo effective z-index — builder process tuần tự, parent luôn có trước child.

---

## Giới hạn đã biết của figma-mcp-go (Phase 1+2)

| Tính năng | Trạng thái |
|---|---|
| Gradient fill native | ✗ Không hỗ trợ → rasterize toàn bộ |
| Image fill native | ✗ → `import_image` tạo image node |
| Per-run text styling (inline bold span, color span) | ✗ → concat thành 1 text node, dùng style của run đầu, warning |
| Conic gradient, clip-path, mask, CSS filter | → raster fallback |
| SVG (vector) | → raster PNG, không editable trong Figma |
| Background-clip: text + transparent fill (gradient text) | → raster |

Khi extractor gặp các case này, nó tự fallback raster và ghi warning vào spec.

---

## Layer Naming Convention

Tự động từ extractor:
- `[<id_or_first_class>/<type>]` cho shape: `[card-glass/frame]`, `[rounded-full/ellipse]`
- `[<id_or_first_class>/Text-<preview>]` cho text: `[text-xs/Text-Milestone]`
- `[<id_or_first_class>/Image]` cho raster
- `[<parent>/BG-Gradient]` cho gradient bg PNG sinh tự động

---

## Lưu ý kỹ thuật

- venv: `.venv/bin/python`
- figma-mcp-go config: `.mcp.json` (Claude session) + spawn riêng từ builder
- Extractor tự pause CSS animations/transitions trước khi screenshot (tránh "element is not stable")
- Builder spawn instance MCP riêng — figma-mcp-go có cơ chế LEADER/FOLLOWER, an toàn chạy song song với Claude's MCP
- Coords trong spec đã normalize relative to bounding box của tất cả visible elements
- Output PNG raster ở 2x DPI cho crisp visual

## Khi user yêu cầu fix kết quả Figma sau khi build

KHÔNG re-run pipeline từ đầu. Thay vào đó:
1. Đọc `<scene>_report.json` để lấy frame_id + uid_to_node_id
2. Dùng MCP tools (`move_nodes`, `resize_nodes`, `set_fills`, etc.) sửa node trực tiếp
3. Nếu lỗi fundamental trong extraction → sửa `html_extractor.py` rồi re-run cả 2 bước
