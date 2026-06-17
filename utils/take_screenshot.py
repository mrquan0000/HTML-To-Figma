#!/usr/bin/env python3
"""
Utility script to capture a screenshot of a specific Figma node/frame.
Saves the result as a PNG file.

Usage:
    python utils/take_screenshot.py --node 8:2 --output output/scene_2_screenshot.png
"""

import argparse
import base64
import json
import sys
from pathlib import Path

# Add the project root directory to the python path to import MCPClient
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))
from agents.figma_builder import MCPClient


def main():
    ap = argparse.ArgumentParser(description="Capture Figma node screenshot")
    ap.add_argument("--node", required=True, help="Figma Node ID (e.g. '8:2')")
    ap.add_argument("--output", required=True, help="Output PNG path")
    ap.add_argument("--mcp-cmd", default="npx", help="MCP command")
    ap.add_argument("--mcp-args", default="-y,@vkhanhqui/figma-mcp-go@latest", help="MCP command arguments")
    args = ap.parse_args()

    output_path = Path(args.output)
    cmd = [args.mcp_cmd, *args.mcp_args.split(",")]

    print(f"Connecting to figma-mcp-go to screenshot node {args.node}...", file=sys.stderr)
    try:
        with MCPClient(cmd, log_stderr=False) as client:
            res = client.call_tool("get_screenshot", {"nodeIds": [args.node]})
            
            # The tool returns content array, containing a text item which is a JSON string.
            content = res.get("content", [])
            if not content:
                print("Error: No content returned from get_screenshot tool.", file=sys.stderr)
                return 1
            
            data_str = content[0].get("text", "")
            if not data_str:
                print("Error: Empty response text from get_screenshot tool.", file=sys.stderr)
                return 1
            
            data = json.loads(data_str)
            exports = data.get("exports", [])
            if not exports:
                print("Error: No exports found in screenshot response.", file=sys.stderr)
                return 1
            
            b64_data = exports[0].get("base64", "")
            if not b64_data:
                print("Error: No base64 data found in export.", file=sys.stderr)
                return 1
            
            img_data = base64.b64decode(b64_data)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(img_data)
            print(f"Screenshot successfully saved to {output_path}", file=sys.stderr)
            return 0
            
    except Exception as e:
        print(f"Failed to take screenshot: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
