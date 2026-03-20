from __future__ import annotations

import argparse
from pathlib import Path


def looks_like_cookie_row(line: str) -> bool:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 7:
        return False
    domain, include_subdomains, path, secure, expires, name = parts[:6]
    if not domain:
        return False
    if include_subdomains not in {"TRUE", "FALSE"}:
        return False
    if not path.startswith("/"):
        return False
    if secure not in {"TRUE", "FALSE"}:
        return False
    if not expires.isdigit():
        return False
    if not name:
        return False
    return True


def normalize_cookie_file(source: Path, target: Path) -> tuple[int, int]:
    raw_lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
    output_lines: list[str] = []
    repaired = 0
    skipped = 0
    pending: str | None = None

    for line in raw_lines:
        if not line.strip():
            continue

        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            if pending is not None:
                if looks_like_cookie_row(pending):
                    output_lines.append(pending)
                else:
                    skipped += 1
                pending = None
            output_lines.append(line)
            continue

        if pending is None:
            pending = line
            continue

        if looks_like_cookie_row(line):
            if looks_like_cookie_row(pending):
                output_lines.append(pending)
            else:
                skipped += 1
            pending = line
            continue

        pending = f"{pending}{line.strip()}"
        repaired += 1

    if pending is not None:
        if looks_like_cookie_row(pending):
            output_lines.append(pending)
        else:
            skipped += 1

    if not output_lines or not output_lines[0].startswith("# Netscape HTTP Cookie File"):
        output_lines.insert(0, "# Netscape HTTP Cookie File")

    target.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return repaired, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize and repair a Netscape cookies.txt file.")
    parser.add_argument("source", nargs="?", default="cookie.txt", help="Source cookies file")
    parser.add_argument(
        "-o",
        "--output",
        default="cookie.repaired.txt",
        help="Output path for repaired cookies file",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    target = Path(args.output).expanduser().resolve()

    if not source.exists():
        raise SystemExit(f"Source file not found: {source}")

    repaired, skipped = normalize_cookie_file(source, target)
    print(f"Repaired lines merged: {repaired}")
    print(f"Skipped invalid rows: {skipped}")
    print(f"Saved repaired file to: {target}")


if __name__ == "__main__":
    main()
