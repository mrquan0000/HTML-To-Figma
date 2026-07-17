# HTML-To-Figma — Claude Instructions (v2)

## Mục đích
Nhận HTML → render trong Chromium headless → extract layout/styles → emit **spec.json v2** → **figma_builder.py** gọi figma-mcp-go dựng frame trên Figma.

Pixel-perfect cho phần Figma vẽ được natively; raster PNG fallback cho phần phức tạp (SVG icons, gradients, filters, clip-path). Mục tiêu fidelity: ~95%+.

## Cách dùng

### Khi user cung cấp đường dẫn FILE HTML, ví dụ:
- `create figma from /path/to/file.html`
- `tạo figma từ input/scene_1.html`
- `html to figma ~/Downloads/page.html`

→ Chạy 2 lệnh ở **Bước 1 + Bước 2** tuần tự, KHÔNG dừng, KHÔNG hỏi lại.

### Khi user cung cấp URL trang web, ví dụ:
- `create figma from https://example.com/page`
- `tạo figma từ url https://...`
- `html to figma url https://... selector ".some-class"`

→ Chạy tuần tự **Bước 0 → Bước 1 → (gate Bước 1.5) → Bước 2**. Báo cáo 1 lần khi xong.

⚠️ **Ngoại lệ — gate ảnh tĩnh:** nếu sau Bước 1 phát hiện section chỉ là **ảnh thiết kế sẵn** (designer upload `.jpg/.webp`, không phải HTML/CSS sống — xem Bước 1.5) → **DỪNG trước Bước 2, báo + hỏi user có muốn vẽ không** kèm link ảnh để user tự download. KHÔNG tự chạy Bước 2 trong trường hợp này.

**Selector tip cho URL workflow:**
- Nếu user cho `selector` → truyền vào `--selector "<chuỗi đó>"` ở Bước 0.
- Nếu không có selector → bỏ qua flag (Bước 0 sẽ lấy `document.body` toàn trang).
- Selector phải trỏ tới element CHỨA tất cả content muốn render. Nếu selector chỉ chứa cards mà heading nằm ở widget anh em → heading sẽ mất khỏi output. Cách an toàn: chọn selector parent rộng hơn (vd `.elementor-section`, `.elementor-widget-wrap`, hoặc `<section>` cha gần nhất).

---

## Chế độ QC (bật/tắt Bước 3 — Visual Validation)

**Trạng thái hiện tại: `QC_MODE: OFF`**

- `QC_MODE: OFF` (mặc định) → sau Bước 2 build xong, **bỏ qua toàn bộ Bước 3** (screenshot + so sánh render), báo cáo ngay theo mẫu rút gọn ở Bước 4. Dùng khi vẽ hàng loạt scene, ưu tiên tốc độ + tiết kiệm token.
- `QC_MODE: ON` → chạy đủ Bước 3 như mô tả bên dưới trước khi báo cáo. Dùng khi muốn kiểm tra kỹ 1 scene, hoặc sau khi thấy nhiều lỗi tích tụ và muốn rà lại.

**Cách bật/tắt:** user nói "bật QC" / "tắt QC" (hoặc tương đương) → sửa dòng `QC_MODE:` ở trên thành `ON`/`OFF`. Đây là công tắc DUY NHẤT quyết định Bước 3 có chạy hay không — không có cơ chế tự động nào khác (vd không tự bật khi thấy nhiều warning).

---

## Pipeline — 2 bước (FILE input) hoặc 3 bước (URL input)

### Bước 0 (CHỈ KHI input là URL): URL → standalone HTML

```bash
.venv/bin/python agents/url_to_html.py \
    --url <URL> \
    [--selector "<CSS selector>"] \
    --output input/<scene_name>.html
```

Tham số tùy chọn:
- `--viewport WxH` — mặc định `1280x900`
- `--wait-for {load,domcontentloaded,networkidle}` — mặc định `networkidle`
- `--wait-extra-ms <int>` — đợi thêm sau load state để render ổn (mặc định `500`)

**Output:** 1 file `input/<scene_name>.html` standalone — chứa `<base href>` của trang gốc, mọi `<link rel="stylesheet">` + font preconnect/preload + inline `<style>` của trang, cộng với override unhide các class `elementor-invisible` (an toàn cho cả trang không phải Elementor).

File này:
- Mở bằng browser xem trước được (verify visual trước khi build Figma)
- Đưa thẳng vào Bước 1 với `--input <file>` không cần xử lý thêm

### Bước 1: Extract HTML → spec.json

```bash
.venv/bin/python agents/html_extractor.py \
    --input /PATH/TO/FILE.html \
    --output output/<scene_name>_spec.json
```

Tham số tùy chọn:
- `--viewport-width` — **bỏ trống = tự động** detect kích thước design:
  - **Canvas cố định** (element có `width`+`height` px, ≥600px rộng, vd `width:1280px;height:720px`) → frame = đúng canvas đó (1280×720), giữ tỷ lệ, KHÔNG +lề.
  - **Card/responsive** (chỉ có `max-width`, vd 1100px) → frame = content + 100px lề; layout ≤600px giữ 600.
  - Chỉ truyền số khi muốn ép 1 width cụ thể.
- `--assets-dir output/assets/<scene_name>` — nơi lưu PNG fallback

**Output:**
- `output/<scene_name>_spec.json` — spec v2
- `output/assets/<scene_name>/*.png` — raster fallback cho SVG + gradient backgrounds + filter

### Bước 1.5 (CHỈ KHI input là URL): Phát hiện "ảnh thiết kế sẵn" → DỪNG, hỏi user

Nhiều trang (Behance, Dribbble, portfolio…) **KHÔNG phục vụ thiết kế bằng HTML/CSS sống** — designer **upload ảnh đã render sẵn** (`.jpg/.webp`). Khi đó section chỉ là **1 thẻ `<img>`**, mọi chi tiết (chữ, card, icon) đã "nướng" thành pixel → Figma chỉ dựng được **1 frame + 1 image**, KHÔNG có layer riêng lẻ editable. Ảnh kiểu này user **có thể tự download trực tiếp**, không cần dựng Figma.

**Cách phát hiện** — sau Bước 1, đọc `output/<scene>_spec.json`:
- Spec chỉ gồm element `image` (1 hoặc vài cái) phủ gần kín frame, **VÀ**
- KHÔNG có element `text`, gần như không có shape native (cùng lắm 1 `rectangle` placeholder màu đồng nhất — chính là placeholder lazy-load).

→ Đây là **ảnh tĩnh**, không phải HTML/CSS sống.

**Hành vi bắt buộc khi phát hiện ảnh tĩnh:**
1. **DỪNG. KHÔNG tự chạy Bước 2.**
2. Báo cho user, đại ý: *"Section này chỉ là 1 ảnh thiết kế sẵn (designer upload `.jpg/.webp`), không phải HTML/CSS sống → Figma chỉ ra 1 frame + 1 image, không có layer riêng lẻ. Bạn có thể tự download ảnh trực tiếp. Bạn có muốn tôi vẽ vào Figma không?"*
3. Kèm **link ảnh gốc** (`src` của `<img>`, ưu tiên bản phân giải cao nhất trong `srcset`) để user download nếu muốn.
4. Chỉ chạy Bước 2 **SAU KHI user đồng ý vẽ**.

Nếu section là HTML/CSS sống (có `text` + nhiều shape native) → bỏ qua gate này, chạy thẳng Bước 2 như bình thường.

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

> **Lưu ý — KHÔNG còn bước reorder BG thủ công.** Trước đây có "Bước 2.5" đưa các lớp BG xuống đáy vì builder xếp BG đè lên content. Root cause đó (effectiveZ không kế thừa stacking-context) đã được fix tận gốc → builder nay xếp đúng tầng ngay khi build (BG/glow/grid nằm dưới content; lớp `BG-Gradient` có sentinel z luôn ở đáy). Nếu sau này gặp scene bị BG đè content → **sửa tiếp `effectiveZ` (gốc), KHÔNG quay lại reorder thủ công** (vá ngọn).

### Bước 3: Visual validation (CHỈ chạy khi `QC_MODE: ON`)

> Nếu `QC_MODE: OFF` (mặc định, xem mục "Chế độ QC" ở trên) → **bỏ qua toàn bộ bước này**, đi thẳng tới Bước 4 với mẫu báo cáo rút gọn.

Khi `QC_MODE: ON`, sau khi build xong (`status: "ok"`):

1. Chụp screenshot **DUY NHẤT 1 LẦN**: `mcp__figma-mcp-go__get_screenshot` với `nodeIds=[<frame_id>]` để export PNG
2. **Render HTML thật làm ảnh tham chiếu** (KHÔNG đọc source code để đoán):
   ```bash
   .venv/bin/python utils/render_html.py --input <FILE.html> --output output/<scene>_html_render.png
   ```
   So sánh Figma PNG **với ảnh render này**, pixel-to-pixel.

   ⚠️ **Chuẩn "đúng" = Figma trung thực với HTML như browser THỰC SỰ render** — không phải theo intent suy diễn từ CSS/markup. Element render ở height=0 / bị clip / vô hình thì **KHÔNG xuất hiện** trong cả render lẫn Figma → đó là ĐÚNG, không phải "thiếu". TUYỆT ĐỐI không đọc raw HTML/CSS rồi kết luận "lẽ ra phải có khối X" — chỉ so cái render thật cho thấy. (Xem [[faithful-to-html-render]].)
3. Check 5 điểm (so render thật ↔ Figma):
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

Nếu `QC_MODE: OFF` (Bước 3 đã bị bỏ qua):
```
Đã tạo xong:
- Frame: <tên> (<width>×<height>px) — node id <id>
- Native layers: X (rectangle/ellipse/text/frame)
- Raster images: Y (SVG icons + gradient backgrounds + filter elements)
- Warnings: <list từ report.json>
- Validation: SKIPPED (QC_MODE: OFF — nói "bật QC" nếu muốn kiểm tra scene này)
```

Nếu `QC_MODE: ON` (đã chạy Bước 3):
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
| Gradient fill native | ✗ Không hỗ trợ thật (xác nhận qua schema `set_fills`: chỉ nhận solid hex). Gradient **linear/radial đơn giản** trên shape/text nhỏ hơn 95% frame → xấp xỉ 1 màu solid (blend 70% stop đậm + 30% stop sáng theo lightness), giữ native/editable, có warning. Gradient phủ ≥95% frame, conic, hoặc nhiều lớp background chồng nhau → vẫn raster. |
| Image fill native | ✗ → `import_image` tạo image node |
| Per-run text styling (inline bold span, color span) | ✗ → concat thành 1 text node, dùng style của run đầu, warning |
| `filter: blur()` / `drop-shadow()` (kể cả kết hợp) | ✓ Native — map sang effect `LAYER_BLUR`/`DROP_SHADOW` (áp dụng cho leaf lẫn container). Filter khác (hue-rotate, contrast, kết hợp với thứ khác ngoài blur/drop-shadow, ...) vẫn raster. |
| Conic gradient, clip-path, mask | → raster fallback |
| SVG (vector) | → raster PNG, không editable trong Figma |
| Background-clip: text + transparent fill (gradient text) | Gradient linear/radial đơn giản → native (xấp xỉ màu solid, xem hàng Gradient fill native ở trên). Conic/nhiều lớp → raster. |

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
- Extractor xử lý HTML động: dùng Web Animations API tìm keyframe có opacity CAO NHẤT cho mỗi animation rồi seek tới đó (không dùng fixed wait) — animation reveal thường (0→1) vẫn dừng ở 100% như cũ, nhưng animation "lùi/mờ" (vd fadeBack 1→0.22) dừng đúng lúc đỉnh thay vì lúc mờ nhất; sau đó freeze RAF và kill transitions. `utils/render_html.py` dùng logic giống hệt để ảnh QC tham chiếu khớp với Figma build. Animation không có opacity keyframe (thuần transform) vẫn dùng `.finish()` như cũ.
- Builder spawn instance MCP riêng — figma-mcp-go có cơ chế LEADER/FOLLOWER, an toàn chạy song song với Claude's MCP
- Coords trong spec đã normalize relative to bounding box của tất cả visible elements
- Output PNG raster ở 2x DPI cho crisp visual

## Dọn dẹp dự án (Cleanup)

Khi hoàn thành một dự án vẽ Figma và muốn dọn sạch các file HTML và Spec/Report/Assets tạm để chuẩn bị cho dự án mới, bạn chạy lệnh sau:
```bash
.venv/bin/python utils/clean_project.py
```

## Quy tắc fix lỗi: LUÔN fix từ GỐC, không fix từ NGỌN

Khi fix bất kỳ lỗi nào (extraction sai, màu sai, layer sai...):
1. **Điều tra nguyên lý trước.** Tìm ĐÚNG nguyên nhân gốc (root cause): cơ chế nào trong pipeline sinh ra output sai? Đọc code, đọc HTML mẫu, chạy lại + xem PNG/spec để có bằng chứng. KHÔNG đoán.
2. **Fix tại gốc**, nơi giá trị sai được tạo ra — không vá ở chỗ triệu chứng xuất hiện. Vá triệu chứng (sai đâu sửa đó) sẽ đẻ ra lỗi mới và che mất bug thật.
3. Một root cause → một fix. Không bundle nhiều thay đổi "tiện tay".
4. Sau khi xác định root cause, trình bày cho user: lỗi do đâu, fix ở đâu, rồi mới làm.

Quy trình: dùng skill `superpowers:systematic-debugging` (Phase 1 root cause → Phase 4 fix).

## Khi user yêu cầu fix kết quả Figma sau khi build

KHÔNG re-run pipeline từ đầu nếu lỗi chỉ ở 1 vài node. Thay vào đó:
1. Đọc `<scene>_report.json` để lấy frame_id + uid_to_node_id
2. Dùng MCP tools (`move_nodes`, `resize_nodes`, `set_fills`, etc.) sửa node trực tiếp
3. Nếu lỗi fundamental trong extraction → sửa `html_extractor.py` rồi re-run cả 2 bước (đây là fix từ gốc, ưu tiên hơn vá node)

---

## 6. Quy trình chống thối rữa hệ thống (AI Rot Prevention)

**Bắt buộc:** Mỗi khi bắt đầu một phiên làm việc mới (session mới), AI phải tự động đọc file `rot.md` (ở thư mục gốc project) để kiểm tra lịch bảo trì.

**Nếu quá hạn:**
1. **Dừng lại ngay**, thông báo user: "[ROT WARNING] Lớp X đã quá hạn rà soát (đến hạn từ ngày Y)"
2. **Đề xuất rà soát:**
   - Quét mã nguồn tìm dead code, feature toggle không dùng, test obsolete
   - Kiểm tra API/dependency có bị break (figma-mcp-go, Playwright, Python venv)
   - Cập nhật prompt/logic nếu cần
3. **Xin phép user trước khi xóa/sửa gì** — chỉ đề xuất + nhắc nhở, không tự động fix

**Lịch cron:**
- **Rules & Hooks**: Nhắc nhở ngày 2026-10-12 (+ hàng quý sau)
- **Extractors & Builders**: Nhắc nhở ngày 2026-08-09 (+ hàng 4 tuần)
- **Tests & Validation**: Nhắc nhở ngày 2026-08-09 (+ hàng 4 tuần)
- **Agents**: Nhắc nhở khi có model Claude mới
- **Tools/CLI**: Kiểm tra manual khi gặp lỗi API break
- **Identity**: Nhắc nhở ngày 2027-07-12 (hàng năm)
