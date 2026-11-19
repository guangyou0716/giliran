"""Every UI string has a Malay and a Simplified Chinese translation."""

import re
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import translation

ROOT = Path(__file__).resolve().parent.parent


def ui_strings() -> list[str]:
    found: list[str] = []
    for path in sorted(ROOT.glob("**/templates/**/*.html")):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        found += [m.group(2) for m in re.finditer(r"{%\s*translate\s+([\"'])(.*?)\1", text)]
        for m in re.finditer(
            r"{%\s*blocktranslate[^%]*%}(.*?){%\s*endblocktranslate\s*%}", text, re.S
        ):
            found.append(re.sub(r"{{\s*(\w+)\s*}}", r"%(\1)s", m.group(1)))
    for path in sorted((ROOT / "queues").glob("**/*.py")):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        found += re.findall(r'\b_\(\s*"((?:[^"\\]|\\.)*)"', text)
    return sorted(set(found))


def test_extractor_finds_strings():
    strings = ui_strings()
    assert len(strings) > 100
    assert "Call next" in strings and "%(n)s waiting" in strings


@pytest.mark.parametrize("lang", ["ms", "zh-hans"])
def test_all_strings_translated(lang):
    missing = []
    with translation.override(lang):
        for s in ui_strings():
            if translation.gettext(s) == s and s not in {"Status", "status", "slug", "slot"}:
                missing.append(s)
    assert missing == []


@pytest.mark.parametrize(
    "lang,html_lang,text",
    [
        ("en", "en", "Take a number"),
        ("ms", "ms", "Ambil nombor"),
        ("zh-hans", "zh-hans", "取号"),
    ],
)
@pytest.mark.django_db
def test_join_page_language(client, branch, consult, lang, html_lang, text):
    r = client.get(reverse("join", args=[branch.slug]), HTTP_ACCEPT_LANGUAGE=lang)
    body = r.content.decode()
    assert f'<html lang="{html_lang}">' in body
    assert text in body


@pytest.mark.django_db
def test_language_switch_cookie(client, branch):
    r = client.post(reverse("set_language"), {"language": "ms", "next": "/"})
    assert r.status_code == 302
    assert "Cawangan" in client.get("/").content.decode()
