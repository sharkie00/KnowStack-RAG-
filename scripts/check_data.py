"""校验种子数据与课程配置：9 门新课各 50 条，JSON 均可解析。"""

import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    config = json.load(open(os.path.join(ROOT, "concepts.json"), encoding="utf-8"))
    seeded = [c for c in config["courses"] if c.get("seed")]
    assert len(seeded) == 9, f"expected 9 seeded courses, got {len(seeded)}"

    files = sorted(glob.glob(os.path.join(ROOT, "seed_data", "*.json")))
    assert len(files) == 9, f"expected 9 seed files, got {len(files)}"
    for path in files:
        entries = json.load(open(path, encoding="utf-8"))
        assert len(entries) == 50, f"{os.path.basename(path)}: {len(entries)} entries"
        for title, text in entries:
            assert title.strip(), f"{path}: empty title"
            assert text.strip(), f"{path}: empty text for {title}"

    print(f"seed check ok: {len(seeded)} courses x 50 entries")


if __name__ == "__main__":
    main()
