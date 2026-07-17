# 🛡️ Sổ tay Bảo trì Hệ thống (AI Rot Prevention Schedule)

Lịch rà soát định kỳ nhằm chống lại hiện tượng "thối rữa" (Rot) của pipeline HTML-To-Figma.

**Yêu cầu đối với mọi AI:** Bắt buộc phải đọc file này ở mỗi phiên làm việc mới để kiểm tra các mốc thời gian. Nếu có thành phần quá hạn, AI phải chủ động nhắc nhở người dùng và đề xuất dọn dẹp các mã thừa, pipeline lỗi thời.

## Bảng theo dõi các lớp (Layers)

| Lớp (Layer) | Tốc độ Rot | Ngày cập nhật gần nhất | Ngày đến hạn rà soát | Người phụ trách | Trạng thái / Ghi chú |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Rules & Hooks** | Trung bình (3 tháng) | 2026-07-12 | 2026-10-12 | AI | Kiểm tra lại `CLAUDE.md`, `.claude/settings.json`, pipeline quy tắc |
| **Extractors & Builders** | Cao (2-4 tuần) | 2026-07-12 | 2026-08-09 | AI | `html_extractor.py`, `figma_builder.py`, logic rasterize/optimize, Figma fidelity |
| **Tests & Validation** | Cao (2-4 tuần) | 2026-07-12 | 2026-08-09 | AI | pytest suite, QC test coverage, visual validation scripts |
| **Agents (Claude Model)** | Rất cao (Có model mới) | 2026-07-12 | Khi có model mới | AI | Kiểm tra độ tương thích prompt, nâng cấp lên model mới nhất (hiện: Haiku 4.5) |
| **Tools / CLI** | Cao (Khi API đổi) | 2026-07-12 | Khi cần thiết | AI | Check figma-mcp-go API, Python venv, Chromium headless, Playwright stability |
| **Identity** | Thấp (1 năm) | 2026-07-12 | 2027-07-12 | User / AI | Đánh giá lại định hướng của project (`CLAUDE.md` core purpose) |
| **Memory** (`~/.claude/projects/-Users-mrlam-Projects-HTML-To-Figma/memory/`) | Trung bình (1 - 2 tháng) | (chưa rà soát) | 2026-07-13 | AI | Rà soát `MEMORY.md` + từng memory file (25 file hiện có): xóa memory sai/lỗi thời, gộp memory trùng chủ đề, kiểm tra path/file được nhắc tới còn tồn tại không. |

---

## Lưu ý quan trọng cho AI

Khi thực hiện rà soát bảo trì:
1. **Ngoài cập nhật logic mới**, hãy mạnh dạn đề xuất loại bỏ (Delete) các file, script, hoặc component đã cũ không còn liên quan.
2. **Dead code**, feature toggle không dùng, test case obsolete → gợi ý xóa để dọn dẹp project.
3. **KHÔNG bao giờ tự xóa mà không hỏi user trước** — chỉ đề xuất + nhắc nhở + xin phép.
4. **Nếu phát hiện pipeline bị break** (API thay đổi, dependency lỗi thời) → báo ngay + xin phép fix, đừng để project "chết dần".

---

**Cron schedule:** Các lớp sẽ được nhắc nhở tự động qua scheduled task (xem CLAUDE.md quy tắc #6).
