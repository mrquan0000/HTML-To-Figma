import argparse
import shutil
from pathlib import Path

def clean():
    parser = argparse.ArgumentParser(description="Dọn dẹp file rác dự án HTML-To-Figma")
    parser.add_argument("--keep", help="Danh sách các file muốn giữ lại ở thư mục input/, cách nhau bằng dấu phẩy. Ví dụ: scene_1_.html,scene_2_.html")
    args = parser.parse_args()

    keep_files = set()
    if args.keep:
        keep_files = {name.strip() for name in args.keep.split(",") if name.strip()}

    project_root = Path(__file__).parent.parent.resolve()
    input_dir = project_root / "input"
    output_dir = project_root / "output"
    
    print("🧹 Đang dọn dẹp các file rác phát sinh trong dự án...")
    if keep_files:
        print(f"👉 Giữ lại trong thư mục input/: {', '.join(keep_files)}")
    
    # Clean input
    cleaned_input = False
    if input_dir.exists():
        for item in input_dir.iterdir():
            if item.name == ".DS_Store":
                item.unlink()
                continue
            if item.name in keep_files:
                continue
            if item.is_file() and item.name != ".gitignore":
                item.unlink()
                print(f"   🗑️ Đã xóa: {item.relative_to(project_root)}")
                cleaned_input = True
            elif item.is_dir():
                shutil.rmtree(item)
                print(f"   🗑️ Đã xóa thư mục: {item.relative_to(project_root)}")
                cleaned_input = True
                
    # Clean output
    cleaned_output = False
    if output_dir.exists():
        for item in output_dir.iterdir():
            if item.name == ".DS_Store":
                item.unlink()
                continue
            if item.is_file() and item.name != ".gitignore":
                item.unlink()
                print(f"   🗑️ Đã xóa: {item.relative_to(project_root)}")
                cleaned_output = True
            elif item.is_dir():
                shutil.rmtree(item)
                print(f"   🗑️ Đã xóa thư mục: {item.relative_to(project_root)}")
                cleaned_output = True
                
    if not cleaned_input and not cleaned_output:
        print("✅ Dự án đã sạch sẽ, không có file rác nào khác để dọn.")
    else:
        print("✨ Dọn dẹp hoàn tất! Thư mục 'input/' và 'output/' đã sẵn sàng cho dự án mới.")

if __name__ == "__main__":
    clean()
