import argparse
import json
from pathlib import Path

from src.model import run


def main():
    parser = argparse.ArgumentParser(description="Run the offline six-site model.")
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-seconds", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.input_root, args.output_dir, args.source_seconds), indent=2))


if __name__ == "__main__":
    main()
