"""The filter reads every script a family reads, not only English.

Issue #1: the bad-language filter and the page scorer have to work on
pages in other languages. These tests cover the mechanics (word
boundaries in every script, prefixes, vowel points, accents, disguises)
and the shipped lists (built from one file per language, and never
touching the ordinary words of a neighbouring language).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kosherd import content, language, search, textfold
from kosherd.language import Wordlist

ROOT = Path(__file__).parents[2]
SHIPPED = ROOT / "os-image/files/usr/share/kosher"
SOURCES = ROOT / "os-image/lists"


# -- textfold ------------------------------------------------------------------

def test_scripts_are_told_apart():
    assert textfold.script_of("זונה") == textfold.HEBREW
    assert textfold.script_of("сука") == textfold.CYRILLIC
    assert textfold.script_of("كس امك") == textfold.ARABIC
    assert textfold.script_of("merde") == textfold.LATIN
    assert textfold.script_of("f*ck") == textfold.LATIN


def test_folding_keeps_letters_that_spell_a_different_letter():
    # Accents that mark stress fold; letters that are their own letters do not.
    assert textfold.fold("cabrón bâtard kurwa mać") == "cabron batard kurwa mac"
    assert textfold.fold("coño") == "coño"
    assert textfold.fold("göt Ärger ő") == "göt Ärger ő"
    assert textfold.fold("Scheiße") == "Scheisse"
    assert textfold.fold("ёж") == "еж"


def test_folding_removes_points_and_unifies_arabic_letters():
    assert textfold.fold("זוֹנָה") == "זונה"
    assert textfold.fold("أُخرى") == "اخري"
    assert textfold.fold("شرموطة") == "شرموطه"
    assert textfold.fold("کیر") == "كير"


# -- the bad-language filter ---------------------------------------------------

@pytest.fixture(scope="module")
def sample():
    return Wordlist({
        "זונה": "יצאנית", "חרא": "זבל", "сука": "негодница", "merde": "mince",
        "cabrón": "canalla", "scheiße": "mist", "coño": "caray", "göt": "popo",
        "شرموطة": "سيئة", "خرى": "قذارة", "fuck": "freak", "boob": "chest",
        "geci": "gazember", "lul": "sukkel", "کونی": "[x]",
    })


@pytest.mark.parametrize("text,expected", [
    ("בן זונה!", "בן יצאנית!"),
    ("והזונה הזאת", "והיצאנית הזאת"),          # behind the prefixes
    ("זוֹנָה", "יצאנית"),                       # with vowel points
    ("Сука, блин", "Негодница, блин"),
    ("СУКА", "НЕГОДНИЦА"),                      # case kept in Cyrillic
    ("Merde alors", "Mince alors"),
    ("un cabron, un cabrón", "un canalla, un canalla"),
    ("Scheisse! Scheiße!", "Mist! Mist!"),
    ("يا شرموطة والشرموطه", "يا سيئة والسيئة"),   # article and ta marbuta
])
def test_words_are_found_in_every_script(sample, text, expected):
    cleaned, count = sample.clean(text)
    assert cleaned == expected and count >= 1


@pytest.mark.parametrize("text", [
    "מזונות לשבת",           # זונה inside a longer word
    "cono de helado",         # ñ is not n
    "he got it",              # ö is not o
    "أُخرى",                  # a vowel mark before the letter is mid-word
    "هر کس",                  # Persian 'anyone' shares letters with a curse
    "Kupimy 455 sztuk",       # digits alone are a number, not a-s-s
    "rated 8.5 out of 10",    # 8.5 is not b-s
    "page 1 ul li",           # 1 ul is not l-u-l
    "SHOPEX_8008_REDIRECT",   # 8008 is not boob
    "c1.05-1.49,2.14-3.04,3.24-4.51 c0.56-0.75",  # SVG path data is not geci
])
def test_ordinary_text_in_any_script_is_left_alone(sample, text):
    assert sample.clean(text) == (text, 0)
    assert not sample.contains_any(text)


def test_a_disguise_needs_half_its_letters():
    words = Wordlist({"shit": "junk", "boob": "chest"})
    assert words.clean("sh1t happens")[0] == "junk happens"
    assert words.clean("5h1t")[0] == "junk"
    assert words.clean("8008")[0] == "8008"
    assert words.clean("b00b")[0] == "chest"


def test_short_words_are_matched_as_written(sample):
    # No disguises and no padding below four letters.
    assert sample.clean("what a lul")[0] == "what a sukkel"
    assert sample.clean("l.u.l")[0] == "l.u.l"
    assert sample.clean("1ul")[0] == "1ul"


def test_only_the_scripts_on_the_page_are_scanned(sample):
    scripts = {textfold.script_of(w) for w in sample.replacements}
    assert scripts == {textfold.LATIN, textfold.HEBREW, textfold.CYRILLIC, textfold.ARABIC}
    latin_only = "The quick brown fox jumps over the lazy dog."
    assert [p for p in sample._applicable(latin_only)] == [
        pattern for presence, pattern in sample._patterns if presence is None]


def test_persian_letters_match_an_arabic_entry():
    words = Wordlist({"سكس": "مواد"})
    assert words.clean("فیلم سکس")[0] == "فیلم مواد"


def test_a_pointed_prefix_is_still_a_prefix(sample):
    assert sample.clean("וְזונה")[0] == "וְיצאנית"


# -- the page scorer -----------------------------------------------------------

@pytest.fixture(scope="module")
def scorer():
    return content.load(SHIPPED / "content-terms.json")


@pytest.mark.parametrize("name,least,text", [
    ("a Hebrew tube site", content.NSFW,
     "סרטי סקס חינם: פורנו ישראלי, מצלמות סקס בשידור חי, נערות ליווי."),
    ("a Hebrew escort listing behind prefixes", content.NSFW,
     "לנערות הליווי הדיסקרטיות בדירות הדיסקרטיות של תל אביב."),
    ("a Russian tube site", content.NSFW,
     "Порно онлайн бесплатно: секс видео, голые девушки, порнуха без регистрации."),
    ("a French escort directory", content.NSFW,
     "Escort girls Paris, rencontres coquines et plan cul, massage érotique."),
    ("a Spanish tube site", content.NSFW,
     "Videos porno gratis en español, sexo en vivo, chicas desnudas."),
    ("a German tube site", content.NSFW,
     "Kostenlose Pornos: deutsche Sexfilme, Sexcams, nackte Frauen."),
    ("an Arabic tube site", content.NSFW,
     "افلام سكس عربي مجانا، سكس مصري، نساء عاريات، افلام اباحية."),
    ("an Arabic site with the article glued on", content.NSFW,
     "بالافلام الاباحية والسكس المجاني للكبار فقط."),
    ("a Hebrew swimwear shop", content.IMMODEST,
     "ביקיני, בגדי ים, מונוקיני וטנקיני — קולקציית בגדי הים והחוף."),
    ("a Russian lingerie shop", content.SUGGESTIVE,
     "Эротическое бельё, пеньюар, соблазнительная обнажённая красота."),
    ("a Hungarian tube site", content.NSFW,
     "Ingyen pornó videók, magyar pornó, szex chat, meztelen lányok."),
    ("a Turkish tube site", content.NSFW,
     "Türk porno izle, bedava porno, sikiş izle, çıplak kızlar."),
    ("a Polish tube site typed without accents", content.NSFW,
     "Darmowe porno, polskie porno, nagie dziewczyny, sex kamerki."),
])
def test_explicit_pages_in_other_languages_are_caught(scorer, name, least, text):
    verdict = scorer.score(text)
    assert verdict.at_least(least), f"{name}: {verdict}"


ORDINARY = [
    "פרשת השבוע: הלכות שבת, ל״ט מלאכות ותולדותיהן, עם מקורות.",
    "מתכון לחלה: קמח, שמרים, מים, סוכר ומלח. ללוש היטב ולהניח לתפוח.",
    "מזונות: ברכת בורא מיני מזונות על עוגה ופת הבאה בכיסנין.",
    "משרד החינוך פרסם את לוח החופשות לשנת הלימודים הקרובה.",
    "Рецепт куриной грудки: обжарить, посолить, подавать с гарниром.",
    "Новости экономики: курс рубля, цены на нефть и ставка ЦБ.",
    "Урок анатомии: мышцы бедра и голени, скелет человека.",
    "Recette de poulet rôti: assaisonner, enfourner, laisser reposer.",
    "Les Enfoirés donnent un concert; il s'est cassé le bras.",
    "Le romans et les contes du Moyen Âge, un cours de littérature.",
    "Receta de pechuga de pollo a la plancha con ensalada.",
    "El delantero marcó dos goles y el equipo ganó la liga; un cono de helado.",
    "O ministro pede arquivamento; o molho fica pronto em 30 minutos.",
    "Pina colada sem álcool: abacaxi, leite de coco e gelo.",
    "Johann Peter Hebel wurde 1760 in Basel geboren; ein helles Zimmer.",
    "Wohnung zu vermieten: drei Zimmer, hell und ruhig, ab sofort.",
    "La guerra di Troia raccontata da Omero; il treno è in ritardo.",
    "Woningen te huur in Amsterdam: hoe werkt het? Kussen en dekbedden.",
    "Kamer vraagt kabinet af te zien van verhogen verkeersboetes.",
    "A csirkemell receptje: sózzuk, borsozzuk, süssük 20 percig.",
    "Przepis na pierś z kurczaka: uda się każdemu, figi na deser.",
    "الأخبار الأخرى: المحكمة العليا وكون القرار نهائيا في شهر مايو.",
    "هر کس که این کتاب را بخواند، زنی که بچه را بزرگ می کند.",
    "Kullanıcıların sık sık incelediği ürünler; kategoriler arasında geçiş.",
    "git commit -m 'fix the build'; mayo on the sandwich; a hell of a game.",
    "A picture of the pics from the con; string and body in the code.",
]


@pytest.mark.parametrize("text", ORDINARY, ids=range(len(ORDINARY)))
def test_ordinary_text_in_other_languages_is_clean(scorer, text):
    verdict = scorer.score(text)
    assert verdict.level == content.CLEAN, f"{verdict} for {text!r}"


@pytest.mark.parametrize("text", ORDINARY, ids=range(len(ORDINARY)))
def test_ordinary_text_in_other_languages_is_not_rewritten(text):
    words = language.load(SHIPPED / "wordlist.json")
    cleaned, count = words.clean(text)
    assert (cleaned, count) == (text, 0), f"{text!r} became {cleaned!r}"


def test_help_context_speaks_other_languages(scorer):
    # A page about the problem needs twice the evidence, in Hebrew too.
    plain = "פורנו ועירום"
    helping = "התמכרות לפורנו: איך להיגמל, טיפול וסינון. ועירום"
    assert scorer.score(plain).at_least(content.NSFW)
    assert not scorer.score(helping).at_least(content.NSFW)
    assert content.is_help_context("adicción a la pornografía, ayuda")
    assert content.is_help_context("зависимость от порно, лечение")


def test_a_search_for_help_in_hebrew_runs():
    blocklist = search.load_blocklist(SHIPPED / "search-blocklist.json")
    assert blocklist.contains_any("פורנו")
    assert blocklist.contains_any("سكس")
    assert blocklist.contains_any("порно онлайн")
    assert content.is_help_context("עזרה בהתמכרות לפורנו")


def test_a_page_cut_inside_a_script_does_not_leak_source():
    head = "<html><head><title>News</title></head><body><p>Hello</p>"
    script = "<script>var sizes=['xx-large','xxx-large'];" + "x=1;" * 5000
    text = content.visible_text(head + script, limit=len(head) + 200)
    assert "xxx" not in text and "Hello" in text
    # And a tag cut in the middle, the way a 50 KB SVG path is.
    path = '<svg><path d="M1,2c0.56-0.75,1.36-1.4' + "c0.5-0.7," * 1000
    text = content.visible_text(head + path, limit=len(head) + 300)
    assert "c0.56" not in text and "Hello" in text
    # A closed script that mentions "<script" in a string is still dropped
    # from its real opening, not from the string.
    tricky = "<script>a='<script';" + "b=2;" * 3000
    text = content.visible_text(head + tricky, limit=len(head) + 400)
    assert "b=2" not in text and "Hello" in text


# -- the shipped lists ---------------------------------------------------------

def test_the_shipped_lists_are_built_from_the_language_sources():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build-lists.py"), "--check"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def language_files():
    return sorted(p for p in SOURCES.glob("*.json") if p.name != "headers.json")


@pytest.mark.parametrize("path", language_files(), ids=lambda p: p.stem)
def test_every_language_source_is_well_formed(path):
    doc = json.loads(path.read_text())
    assert doc["language"] == path.stem and doc["name"] and doc["comment"]
    assert doc["replacements"], "a language with no bad language to clean"
    for word, replacement in doc["replacements"].items():
        assert word.strip() and replacement.strip(), (path.stem, word)
        # Every entry must at least find itself.
        assert Wordlist({word: replacement}).clean(word)[0] == replacement, word
    for level, by_weight in doc.get("content", {}).items():
        assert level in content.SEVERITY and level != content.CLEAN
        for weight, terms in by_weight.items():
            assert int(weight) in (25, 12, 8, 6, 4, 3), (level, weight)
            assert all(t.strip() for t in terms)


def test_the_shipped_lists_cover_every_language():
    for name in ("wordlist.json", "content-terms.json", "search-blocklist.json"):
        doc = json.loads((SHIPPED / name).read_text())
        assert set(doc["languages"]) == {p.stem for p in language_files()}
    words = language.load(SHIPPED / "wordlist.json")
    scripts = {textfold.script_of(w) for w in words.replacements}
    assert scripts == {textfold.LATIN, textfold.HEBREW, textfold.CYRILLIC, textfold.ARABIC}
    assert len(words) > 1000


def test_the_shipped_list_still_loads_fast_enough():
    """The whole list compiles in about a second and a 27 KB page scans in
    about ten milliseconds; a guard on the shape (first-letter factoring
    and lazy per-word patterns), measured loosely."""
    import time
    t = time.perf_counter()
    words = language.load(SHIPPED / "wordlist.json")
    assert time.perf_counter() - t < 6
    assert not words._each, "per-word patterns must not be compiled at load"
    page = "The quick brown fox jumps over the lazy dog. " * 600
    t = time.perf_counter()
    words.clean(page)
    assert time.perf_counter() - t < 0.2
