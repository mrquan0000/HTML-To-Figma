# Hướng dẫn sinh HTML để tối ưu độ trung thực khi dựng Figma

> **Cho:** bên Video Editor (phần sinh HTML từ AI).
> **Mục đích:** giúp HTML sinh ra dựng lên Figma **trung thực + nhiều layer editable**, giảm raster và giảm sai lệch Chrome↔Figma.
> **Bối cảnh:** rút ra từ đợt test 6 model (Claude/Gemini/GPT/DeepSeek/GLM) build thật vào Figma 2026-07-10.

---

## ⚠️ RÀNG BUỘC BẮT BUỘC: KHÔNG ĐƯỢC PHÁ ANIMATION (Remotion + editor)

HTML bên video editor sinh ra **có animation** phục vụ: (1) dựng/preview trong editor, và (2) **render video bằng project `video_remotion`**. Mọi phối hợp dưới đây **PHẢI giữ nguyên animation**.

**Nguyên tắc vàng:**
> Chỉ đổi các thuộc tính **TĨNH, không liên quan chuyển động** (font, cấu trúc text, loại gradient nền, ngắt dòng). **TUYỆT ĐỐI KHÔNG** bỏ/sửa `@keyframes`, `animation`, `transition`, `transform`, `filter`... nếu chúng đang tạo chuyển động.

**Vì sao an toàn cho Figma:** pipeline Figma **tự động freeze mọi animation về đúng end-state** (seek tới keyframe đỉnh, freeze RAF, kill transition) trước khi extract, và tự raster phần nào không vẽ native được. Nên **animation KHÔNG cản trở Figma** — không cần bỏ chúng đi vì Figma.

Mỗi mục dưới đây gắn nhãn:
- 🟢 **TĨNH — đổi an toàn** (không đụng animation)
- 🔴 **ĐỘNG — GIỮ NGUYÊN** (Figma tự xử lý, đừng đổi vì Figma)

---

## 1. 🟢 FONT — đòn bẩy quan trọng nhất (đổi an toàn, lại còn lợi cho Remotion)

**Vấn đề:** Figma chỉ render đúng nếu font được cài trong Figma. Font Figma **không có** → Figma **thay font khác rộng/hẹp hơn** → chữ wrap lệch, tràn dòng, đè nội dung. Trong test, heading dùng `"Segoe UI"` bị thay font rộng hơn → 2 dòng thành 3 dòng, đè subtitle.

**Lợi kép cho Remotion:** `video_remotion` render bằng **Chromium headless trên server** — server đó cũng **không có** font hệ thống (`Segoe UI`, `-apple-system`...) → cũng bị fallback y hệt. Dùng **web font nạp qua `@font-face`/Google Fonts** giúp **cả Remotion lẫn Figma** render đúng và giống nhau. Đây là thay đổi tĩnh, **không đụng animation**.

### ✅ NÊN — web font, khai báo tường minh
```
Inter, Poppins, Roboto, Arial, Montserrat
```
(Figma bản này đang có: **Inter, Poppins, Arial**. Cần thêm font khác → cài vào Figma trước rồi báo cập nhật whitelist. Nhớ đảm bảo Remotion cũng nạp được font đó qua `@font-face`.)

### ❌ TRÁNH — font/stack hệ thống
```
system-ui, -apple-system, "Segoe UI", "Helvetica Neue", BlinkMacSystemFont, ui-sans-serif
```
- Khai báo **1 family tường minh**: `font-family: 'Inter', sans-serif;` — không stack dài trộn font hệ thống.
- Weight nằm trong bộ Figma có (Inter: Regular/Medium/SemiBold/Bold/Extra Bold/Black).

---

## 2. 🟢 TEXT — tránh mất màu / sai wrap (đổi an toàn)

### Inline styling nhiều màu trong 1 khối text
figma-mcp-go **không** styling theo range → 1 text node = **1 style**. Heading có `<span>` đổi màu 1 từ giữa câu → Figma **gộp về 1 màu** (từ tô màu **mất màu**).
```html
<!-- ❌ từ "tốc độ" MẤT màu cam khi lên Figma -->
<h1>Trải nghiệm <span style="color:orange">tốc độ</span> cao</h1>
```
→ Nếu cần nhấn màu 1 cụm chữ: đặt cụm nhấn ở **dòng/khối riêng** thay vì inline giữa câu.
→ **Lưu ý animation:** nếu cụm chữ đó đang được animate riêng thì **cứ giữ nó là 1 element riêng** (điều này tốt cho cả Remotion lẫn Figma) — chỉ tránh kiểu inline-span-đổi-màu-giữa-câu.

### Wrap dòng: ưu tiên ngắt dòng cứng
- Chrome↔Figma đo chữ lệch vài px → dòng "gần đầy" wrap khác nhau.
- Muốn heading xuống dòng đúng chỗ → **dùng `<br>` (ngắt cứng)**, đừng để tự wrap sát mép. (Ngắt cứng là tĩnh, không ảnh hưởng animation.)

---

## 3. 🟢 NỀN & GRADIENT TĨNH (đổi an toàn — nếu gradient KHÔNG bị animate)

> Chỉ áp dụng cho gradient **tĩnh**. Nếu gradient đang được animate (vd xoay conic, chạy `background-position`) cho video → xem mục 4, **giữ nguyên**.

### ✅ Native (editable)
- Solid: `background:#1a1a2e;`
- Gradient **linear/radial đơn giản** trên shape nhỏ (<95% frame) → editable nhưng **xấp xỉ 1 màu solid**.

### ⚠️ Thành ảnh (hiện đúng, không editable)
- Gradient nền **full-frame** (≥95%) → 1 layer ảnh BG.
- **Radial fade ra trong suốt** (glow) → raster để giữ độ mềm (chủ đích).

### ❌ TRÁNH (nếu là nền tĩnh) — buộc raster nhiều
- `conic-gradient`, `repeating-*-gradient`, nhiều lớp background chồng, `background:url(...)`
- `background-clip:text` với gradient phức tạp (conic/nhiều lớp) → cả khối chữ thành ảnh. (Linear/radial đơn giản thì OK.)

---

## 4. 🔴 TRANSFORM / FILTER / 3D / ANIMATION — GIỮ NGUYÊN, đừng đổi vì Figma

**Đây là vùng tạo chuyển động cho Remotion — KHÔNG bỏ, KHÔNG sửa vì Figma.** Pipeline Figma tự freeze về end-state và tự raster phần cần thiết.

- `@keyframes`, `animation`, `transition` → **giữ**. Figma tự seek tới đỉnh + freeze.
- `transform` 2D/3D (`translate/scale/rotate/rotateX/Y/matrix3d/perspective`) → **giữ**. Figma tự xử lý; nếu là 3D thật thì raster về end-state (vẫn đúng khung hình).
- `filter` (`blur`, `drop-shadow`, kể cả `hue-rotate`/`contrast`...) → **giữ**. blur/drop-shadow map native; loại khác raster.
- Phần tử xoay/biến hình khi animate → **giữ**; Figma raster về trạng thái cuối.

**Tóm lại mục 4:** bên video editor **không cần làm gì** ở đây cho Figma — cứ tối ưu cho animation/Remotion như hiện tại.

---

## 5. 🟢 LAYOUT — badge / nút / pill (đổi an toàn)

- Nút/badge có **padding + icon con** đã được pipeline đo đúng vị trí chữ → **không cần né**, cứ dùng `padding` + `inline-flex` + icon bình thường.
- Icon `display:block` trong nút hẹp có thể đẩy label xuống dòng → để icon là flex item và chừa đủ rộng cho label.

---

## 6. Checklist nhanh (chỉ các mục 🟢 TĨNH — không đụng animation)

- [ ] Font: chỉ `Inter / Poppins / Roboto / Arial / Montserrat`, khai báo tường minh, **không** stack hệ thống. *(Lợi cho cả Remotion.)*
- [ ] Không nhấn màu 1 từ bằng `<span>` inline giữa câu (mất màu ở Figma).
- [ ] Muốn xuống dòng heading → dùng `<br>`, đừng để tự wrap sát mép.
- [ ] Nền **tĩnh**: solid / linear / radial đơn giản. Tránh conic/multi-layer/`url()`. *(Nền động thì giữ nguyên.)*
- [ ] **KHÔNG** đụng `@keyframes`/`animation`/`transform`/`filter` — Figma tự xử lý.

---

*Ghi chú: pipeline Figma vẫn tự fallback raster + freeze animation cho mọi HTML, nên HTML "không theo" vẫn dựng được — chỉ ít layer editable hơn và có thể lệch nhẹ. Theo checklist (các mục tĩnh) = Figma đẹp + editable nhất, mà **không hề ảnh hưởng animation của Remotion**.*
