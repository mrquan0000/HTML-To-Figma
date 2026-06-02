# HTML-To-Figma — Claude Instructions

## Mục đích
Nhận file HTML (do Gemini generate) → render chính xác trong browser headless → extract tọa độ/màu sắc/effects → dựng Figma layers qua figma-mcp-go. Fidelity mục tiêu: ~95%+ (tọa độ chính xác hoàn toàn, không estimate).

## Cách dùng
Khi user cung cấp đường dẫn HTML, ví dụ:
- `create figma from /path/to/file.html`
- `tạo figma từ input/card.html`
- `html to figma ~/Downloads/gemini-output.html`

→ Chạy pipeline bên dưới từ đầu đến cuối **KHÔNG DỪNG, KHÔNG hỏi lại**
→ Báo cáo 1 lần khi xong

---

## Pipeline — BẮT BUỘC THEO ĐÚNG THỨ TỰ

### Step 1: Extract HTML spec bằng Playwright

```bash
.venv/bin/python agents/html_extractor.py --input /PATH/TO/FILE.html --output output/spec.json
```

→ Ghi nhận `frame_width`, `frame_height`, danh sách `elements` từ JSON output.
→ Tọa độ x,y,w,h là **tuyệt đối chính xác** từ browser — KHÔNG ước lượng, KHÔNG điều chỉnh thêm.

### Step 2: Đọc và phân tích spec

Đọc `output/spec.json`. Với mỗi element ghi nhận:
- `type`: rectangle | text
- `x`, `y`, `width`, `height`
- `fill_color` (#RRGGBB) hoặc `gradient`
- `stroke_color`, `stroke_width`
- `corner_radius`
- `opacity`
- `shadow` (nếu có): type, color, opacity, offset_x, offset_y, blur, spread
- `backdrop_blur` (nếu có)

**CSS → Figma mapping:**

| Spec field | Figma tool |
|---|---|
| `fill_color` (solid) | `set_fills` type=SOLID |
| `gradient` (linear_gradient) | `set_fills` type=GRADIENT_LINEAR |
| `stroke_color` + `stroke_width` | `set_strokes` |
| `corner_radius` | `set_corner_radius` |
| `opacity` < 100 | `set_opacity` |
| `shadow.type` = DROP_SHADOW | `set_effects` type=DROP_SHADOW |
| `shadow.type` = INNER_SHADOW | `set_effects` type=INNER_SHADOW |
| `backdrop_blur` | `set_effects` type=BACKGROUND_BLUR |

### Step 3: Xây group map theo DOM

Nhìn vào hierarchy của elements (xem tọa độ bounding box lồng nhau):
- Element con nằm **hoàn toàn bên trong** element cha → cùng group
- Ví dụ: card container → tất cả badge/text/items bên trong = 1 group
- Background full-frame → group "Background"

Đặt tên groups semantic: `Card`, `Badge`, `Header`, `ListItems`, `Footer`, `Background`, v.v.

### Step 4: Kiểm tra canvas Figma

```
get_document (hoặc scan_nodes_by_types)
```
- Nếu frame cùng tên đã tồn tại → xóa trước (`delete_nodes`)
- Đặt frame mới tại `x = max_existing_x + 200, y = 0`

### Step 5: Tạo Figma frame

```
create_frame(name=frame_name, width=frame_width, height=frame_height)
```
Ghi nhận `frame_id`.

### Step 6: Tạo elements (bottom to top)

Thứ tự: Background rect → Panel/Card BGs → Content (text, labels) → Decorative

**Với mỗi rectangle:**
1. `create_rectangle(parentId=frame_id)`
2. `resize_nodes` → width, height
3. `move_nodes` → x, y
4. `set_fills`:
   - Solid: `[{type: "SOLID", color: {r, g, b}}]` (chuyển hex sang 0–1 float)
   - Gradient: `[{type: "GRADIENT_LINEAR", gradientStops: [...], gradientTransform: [...]}]`
5. `set_strokes` nếu stroke_width > 0
6. `set_corner_radius` nếu > 0
7. `set_opacity` nếu < 100
8. `set_effects` nếu có shadow hoặc backdrop_blur:
   ```
   [{
     type: "DROP_SHADOW",
     color: {r, g, b, a},   ← a = opacity/100
     offset: {x: offset_x, y: offset_y},
     radius: blur,
     spread: spread
   }]
   ```
9. `rename_node` → tên từ spec

**Với mỗi text:**
1. `create_text(parentId=frame_id)`
2. `resize_nodes` → width, height
3. `move_nodes` → x, y
4. `set_text` → text_content
5. `set_fills` → text_color (dùng trên text node)
6. `rename_node` → tên từ spec

**Màu RGB:** hex `#E8973F` → `r=0.910, g=0.592, b=0.247` (chia 255)

### Step 7: Group nodes

```
group_nodes(nodeIds=[id1, id2, ...], parentId=frame_id)
rename_node(nodeId=group_id, name="[GroupName]")
```
Layer order: Background ở bottom, content ở top.

### Step 8: Visual validation (1 lần, không loop)

1. `get_screenshot` của frame vừa tạo
2. Đọc screenshot bằng Read tool
3. Mở HTML gốc bằng Read tool để so sánh
4. Kiểm tra 5 điểm nhanh:
   - [ ] Gradient background đúng màu?
   - [ ] Border + corner_radius hiện đúng?
   - [ ] Shadow hiển thị?
   - [ ] Text đúng vị trí, đúng màu?
   - [ ] Kích thước frame khớp HTML?
5. Sửa 1 lần nếu thấy lệch rõ → ghi vào report nếu còn issue

### Step 9: Report

```
Đã tạo xong:
- Frame: <tên> (<width>×<height>px)
- Elements: X layers
- Groups: Y groups ([tên group 1], [tên group 2], ...)
- Effects: Z
- Validation: [PASS hoặc list issues]
```

---

## Xử lý gradient fill trong Figma

CSS: `linear-gradient(135deg, #200F00, #542A06)`

Figma `set_fills`:
```json
[{
  "type": "GRADIENT_LINEAR",
  "gradientStops": [
    {"color": {"r": 0.125, "g": 0.059, "b": 0.0, "a": 1.0}, "position": 0.0},
    {"color": {"r": 0.329, "g": 0.165, "b": 0.024, "a": 1.0}, "position": 1.0}
  ],
  "gradientTransform": [[0.707, -0.707, 0.146], [0.707, 0.707, -0.146]]
}]
```
(135deg → transform matrix: cos135=−0.707, sin135=0.707)

---

## Layer Naming Convention

```
[Background/Rect-BG]
[Card/Rect-CardBG]
[Card/Badge/Rect-BadgePill]
[Card/Badge/Text-BadgeLabel]
[Card/Header/Text-Title]
[Card/ListItems/Text-Item1]
[Card/Footer/Rect-Divider]
```

---

## Lưu ý

- venv path: `.venv/bin/python`
- figma-mcp-go config: `.mcp.json`
- Viewport default: 600px (khớp HTML max-width thường dùng)
- Nếu HTML dùng viewport khác (ví dụ 1200px) → thêm `--viewport-width 1200`
- `output/spec.json` là file trung gian, không commit
