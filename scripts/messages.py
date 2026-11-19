"""Compile locale/*/LC_MESSAGES/django.po to .mo with Babel (no GNU gettext needed).

python scripts/messages.py           # check .po validity and that the .mo files are current
python scripts/messages.py --write   # rewrite the .mo files from the .po files
"""

import io
import sys
from pathlib import Path

from babel.messages.mofile import read_mo, write_mo
from babel.messages.pofile import read_po

LOCALE = Path(__file__).resolve().parent.parent / "locale"


def messages(catalog) -> dict:
    """msgid -> msgstr, header excluded (Babel stamps dates into the header)."""
    return {m.id: m.string for m in catalog if m.id and not m.fuzzy}


def compile_po(po_path: Path):
    with po_path.open("rb") as fh:
        catalog = read_po(fh, abort_invalid=True)
    problems = []
    for message, errors in catalog.check():
        for error in errors:
            problems.append(f"{po_path}:{message.lineno}: {error}")
    untranslated = [m.id for m in catalog if m.id and not m.string]
    problems += [f"{po_path}: untranslated: {mid!r}" for mid in untranslated]
    buf = io.BytesIO()
    write_mo(buf, catalog)
    return buf.getvalue(), messages(catalog), problems


def _mo_messages(mo_path: Path) -> dict:
    with mo_path.open("rb") as fh:
        return messages(read_mo(fh))


def main(argv: list[str]) -> int:
    write = "--write" in argv
    failed = False
    po_files = sorted(LOCALE.glob("*/LC_MESSAGES/django.po"))
    if not po_files:
        print("no .po files found", file=sys.stderr)
        return 1
    for po in po_files:
        data, expected, problems = compile_po(po)
        for p in problems:
            print(p, file=sys.stderr)
        failed |= bool(problems)
        mo = po.with_suffix(".mo")
        if write:
            mo.write_bytes(data)
            print(f"wrote {mo.relative_to(LOCALE.parent)}")
        elif not mo.exists() or _mo_messages(mo) != expected:
            print(f"{mo}: stale, run python scripts/messages.py --write", file=sys.stderr)
            failed = True
        else:
            print(f"ok {mo.relative_to(LOCALE.parent)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
