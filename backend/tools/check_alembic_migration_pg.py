from __future__ import annotations

"""
Quick static checks for Alembic migrations intended for PostgreSQL.

Checks:
- FK order: create_table(A) contains ForeignKey("B.id") but B is created later
- sqlite-ish defaults tokens: datetime(, strftime(, julianday(, date(
- boolean defaults using 0/1 in server_default=sa.text("0|1") near Boolean columns
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0001_init.py"


def main() -> int:
    text = MIGRATION.read_text(encoding="utf-8")

    # Table creation order
    create_pat = re.compile(r'op\.create_table\(\s*\n\s*"([^"]+)"', re.M)
    order = create_pat.findall(text)
    idx = {name: i for i, name in enumerate(order)}

    # Split into create_table blocks
    blocks: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in create_pat.finditer(text)]
    blocks.append((len(text), ""))

    fk_pat = re.compile(r"sa\.ForeignKey\(\s*['\"]([^'\"]+)['\"]")
    fk_issues: list[tuple[str, str]] = []
    for (start, name), (end, _) in zip(blocks, blocks[1:]):
        block = text[start:end]
        for ref in fk_pat.findall(block):
            ref_table = ref.split(".", 1)[0]
            if ref_table not in idx:
                continue
            if idx[ref_table] > idx[name]:
                fk_issues.append((name, ref_table))

    sqlite_tokens = [t for t in ("datetime(", "strftime(", "julianday(", "date(") if t in text]

    # Boolean 0/1 defaults (both inline and multi-line)
    bool_default_issues: list[str] = []
    bool_pat = re.compile(r"sa\.Boolean\(\)([\s\S]{0,200}?)\)", re.M)
    for m in bool_pat.finditer(text):
        window = text[m.start() : m.start() + 260]
        if 'server_default=sa.text("0")' in window or 'server_default=sa.text("1")' in window:
            # include a short snippet for locating
            snippet = " ".join(line.strip() for line in window.splitlines()[:6])
            bool_default_issues.append(snippet)

    print(f"Migration: {MIGRATION}")
    print(f"Tables: {len(order)}")

    print(f"\nFK order issues: {len(fk_issues)}")
    for a, b in fk_issues:
        print(f"- {a} references {b} but {b} is created later")

    print(f"\nSQLite-ish tokens present: {sqlite_tokens if sqlite_tokens else 'none'}")

    print(f"\nBoolean 0/1 server_default issues: {len(bool_default_issues)}")
    for s in bool_default_issues[:20]:
        print(f"- {s}")
    if len(bool_default_issues) > 20:
        print(f"... and {len(bool_default_issues) - 20} more")

    # Exit code non-zero if any issues found
    return 1 if (fk_issues or sqlite_tokens or bool_default_issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())

