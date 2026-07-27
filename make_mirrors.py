import os
import sys

def main():
    # Resolve paths relative to this script's location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_root = script_dir  # make_mirrors.py lives at workspace root

    # ROE source: navigate up to HAF, then into DCOM
    haf_root = os.path.dirname(os.path.dirname(workspace_root))
    roe_path = os.path.join(haf_root, "DCOM", "docs", "doctrine", "ROE.md")

    if not os.path.exists(roe_path):
        print(f"ERROR: ROE source not found at {roe_path}")
        sys.exit(1)

    with open(roe_path, "r") as f:
        roe = f.read()

    boot_block = """## BOOT BLOCK

On "Continue", follow `CONTINUE.md` (read it, pull, check drift, orient, act).
"""

    workspace_block = """## MarkItDown

This project is a FastAPI backend powered by Microsoft MarkItDown + MinerU.
"""

    full_content = roe + "\n\n" + boot_block + "\n" + workspace_block

    for filename in ["CLAUDE.md", "AGENTS.md"]:
        filepath = os.path.join(workspace_root, filename)
        with open(filepath, "w") as f:
            f.write(full_content)
        print(f"  wrote {filepath}")

    print("Mirrors updated. Verify: cmp -s CLAUDE.md AGENTS.md")

if __name__ == "__main__":
    main()
