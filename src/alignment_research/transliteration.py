"""Deterministic romanization of the source side, for display only.

A reverse interlinear is read by people who cannot read the source script, so
the pointed Hebrew or accented Greek beneath an English word is not much use on
its own. These functions produce a romanization to sit beside it.

Two rules keep this honest. A transliteration is a display aid and never an
alignment input: no head, edge, or textual decision may be taken from it. And
where the source itself supplies one, that value wins — MACULA Hebrew's
transliteration knows things the letters do not show, such as which dagesh
doubles a consonant, so it is preferred and these functions only fill its gaps.

The Hebrew scheme follows MACULA's own conventions so that a filled gap does
not read as a different system: macron for the long vowels, circumflex for a
vowel written with a mater, `ə` for vocal shewa, a breve for the reduced
vowels, `ʾ`/`ʿ` for aleph and ayin, and underlines for spirantized begadkefat.
The Greek scheme is the usual scholarly one: `ē`/`ō` for eta and omega, `h` for
the rough breathing, `ng` for gamma before a velar, and no accents, because the
accents are already visible in the Greek line above.
"""
from __future__ import annotations

import re
import unicodedata


# Hebrew combining points, by their Unicode names rather than their appearance.
SHEVA = "ְ"
HATAF_SEGOL = "ֱ"
HATAF_PATAH = "ֲ"
HATAF_QAMATS = "ֳ"
HIRIQ = "ִ"
TSERE = "ֵ"
SEGOL = "ֶ"
PATAH = "ַ"
QAMATS = "ָ"
HOLAM = "ֹ"
HOLAM_HASER = "ֺ"
QUBUTS = "ֻ"
DAGESH = "ּ"
SHIN_DOT = "ׁ"
SIN_DOT = "ׂ"
QAMATS_QATAN = "ׇ"

HEBREW_VOWELS = {
    SHEVA: "ə",
    HATAF_SEGOL: "ĕ",
    HATAF_PATAH: "ă",
    HATAF_QAMATS: "ŏ",
    HIRIQ: "i",
    TSERE: "ē",
    SEGOL: "e",
    PATAH: "a",
    QAMATS: "ā",
    HOLAM: "ō",
    HOLAM_HASER: "ō",
    QUBUTS: "u",
    QAMATS_QATAN: "o",
}

# A mater lectionis turns the preceding vowel into its written-long form.
HEBREW_MATER = {
    "ō": "ô",
    "ū": "û",
    "u": "û",
    "ē": "ē",
    "e": "e",
    "i": "i",
}

HEBREW_CONSONANTS = {
    "א": "ʾ", "ה": "h", "ו": "w", "ז": "z", "ח": "ḥ", "ט": "ṭ", "י": "y",
    "ל": "l", "מ": "m", "ם": "m", "נ": "n", "ן": "n", "ס": "s", "ע": "ʿ",
    "צ": "ṣ", "ץ": "ṣ", "ק": "q", "ר": "r", "ש": "š", "שׁ": "š", "שׂ": "ś",
}

# Begadkefat: hard with a dagesh, spirantized without one.
HEBREW_BEGADKEFAT = {
    "ב": ("b", "ḇ"),
    "ג": ("g", "ḡ"),
    "ד": ("d", "ḏ"),
    "כ": ("k", "ḵ"),
    "ך": ("k", "ḵ"),
    "פ": ("p", "p̄"),
    "ף": ("p", "p̄"),
    "ת": ("t", "ṯ"),
}

# Maqqef joins two words and is written; sof pasuq and paseq separate them and
# are not part of any word, so they are dropped rather than romanized.
HEBREW_PUNCTUATION = {"־": "-"}
HEBREW_SEPARATORS = "׃׀"

GUTTURALS = "חעה"
FINAL_FORMS = "ךםןףץ"
# A written-long vowel, or a mater that makes the preceding one long. A shewa
# after one of these is vocal; a furtive patah needs one before it.
LONG_VOWELS = "āēōûôâîêūī"
MATRES = "yw"


def hebrew_transliteration(pointed: str) -> str:
    """Romanize a pointed Hebrew form.

    The input is one MACULA morpheme, which may be a bare prefix such as `בְּ`
    or a whole word. Accents are ignored: they mark cantillation and stress,
    which the Hebrew line already shows.
    """
    if not pointed:
        return ""
    text = unicodedata.normalize("NFD", pointed)
    pieces: list[str] = []
    previous_shewa_was_silent = False
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in HEBREW_SEPARATORS:
            index += 1
            continue
        if character in HEBREW_PUNCTUATION:
            pieces.append(HEBREW_PUNCTUATION[character])
            index += 1
            continue
        if character not in HEBREW_CONSONANTS and character not in HEBREW_BEGADKEFAT:
            # Cantillation marks, and any vowel left stranded without a
            # consonant of its own, as happens when MACULA splits a word before
            # its pronominal suffix.
            if character in HEBREW_VOWELS:
                pieces.append(HEBREW_VOWELS[character])
            index += 1
            continue

        marks = []
        cursor = index + 1
        while cursor < length and unicodedata.combining(text[cursor]):
            marks.append(text[cursor])
            cursor += 1

        vowels = [mark for mark in marks if mark in HEBREW_VOWELS]
        has_dagesh = DAGESH in marks
        letter = character
        if character == "ש":
            letter = "שׁ" if SHIN_DOT in marks else "שׂ" if SIN_DOT in marks else "ש"

        if character == "ו" and has_dagesh and not vowels:
            # Shuruq: a waw carrying a dot is the vowel, not a consonant.
            pieces.append(_apply_mater(pieces, "û"))
            index = cursor
            continue
        if character == "ו" and vowels and vowels[0] in (HOLAM, HOLAM_HASER):
            pieces.append(_apply_mater(pieces, "ô"))
            index = cursor
            continue

        if character in HEBREW_BEGADKEFAT:
            hard, soft = HEBREW_BEGADKEFAT[character]
            consonant = hard if has_dagesh else soft
            if has_dagesh and pieces and _is_vowel(pieces[-1]):
                # A dagesh after a vowel is forte, so it hardens and doubles;
                # one opening a syllable is lene and only hardens.
                consonant += consonant
        else:
            consonant = HEBREW_CONSONANTS[letter]
            if has_dagesh:
                # A dagesh in a letter that cannot be spirantized doubles it.
                consonant += consonant

        if character == "י" and pieces and not vowels and _is_vowel(pieces[-1]):
            # Consonantal yod after a vowel is a mater: it lengthens the vowel
            # in writing rather than sounding on its own.
            pieces.append("y")
            index = cursor
            continue

        vowel_text = "".join(HEBREW_VOWELS[mark] for mark in vowels)
        if vowel_text == "ā" and text.endswith("־") and _closes_syllable(text, cursor):
            # Qamats qatan: a qamats in a closed unaccented syllable is `o`,
            # as in `kol-`. A maqqef is what makes the syllable unaccented, and
            # requiring it keeps ordinary long qamats untouched.
            vowel_text = "o"
        shewa_is_silent = False
        if vowels == [SHEVA]:
            # A shewa is vocal at the start of a word, under a doubled
            # consonant, after a long vowel, or following a silent shewa.
            # Otherwise it closes the syllable and is not pronounced.
            vocal = (
                not pieces
                or has_dagesh
                or previous_shewa_was_silent
                or _ends_long(pieces[-1])
            )
            if not vocal or (cursor >= length and character in FINAL_FORMS):
                vowel_text = ""
                shewa_is_silent = True
        if (
            vowel_text == "a"
            and character in GUTTURALS
            and cursor >= length
            and pieces
            and (_is_vowel(pieces[-1]) or _ends_long(pieces[-1]))
        ):
            # Furtive patah: written under a final guttural but pronounced
            # before it.
            pieces.append("ᵃ" + consonant)
        else:
            pieces.append(consonant + vowel_text)
        previous_shewa_was_silent = shewa_is_silent
        index = cursor
    return "".join(pieces)


def _is_vowel(piece: str) -> bool:
    return bool(piece) and piece[-1] in "aāeēiīoōuūəĕăŏâêîôû"


def _closes_syllable(text: str, cursor: int) -> bool:
    """Whether the next consonant carries no vowel, so the syllable is closed."""
    while cursor < len(text) and text[cursor] in HEBREW_PUNCTUATION:
        cursor += 1
    if cursor >= len(text):
        return False
    following = cursor + 1
    while following < len(text) and unicodedata.combining(text[following]):
        if text[following] in HEBREW_VOWELS:
            return False
        following += 1
    return True


def _ends_long(piece: str) -> bool:
    """Whether a piece ends in a long vowel or in a mater writing one."""
    return bool(piece) and piece[-1] in LONG_VOWELS + MATRES


def _apply_mater(pieces: list[str], written: str) -> str:
    """Fold a mater into the vowel it writes, rather than adding a letter."""
    if pieces and pieces[-1] and pieces[-1][-1] in HEBREW_MATER:
        previous = pieces[-1]
        pieces[-1] = previous[:-1] + HEBREW_MATER[previous[-1]]
        return ""
    return written


GREEK_LETTERS = {
    "α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "ē",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "ph", "χ": "ch", "ψ": "ps", "ω": "ō",
}

GREEK_DIPHTHONGS = {
    "αι": "ai", "ει": "ei", "οι": "oi", "υι": "ui",
    "αυ": "au", "ευ": "eu", "ηυ": "ēu", "ου": "ou",
}

ROUGH_BREATHING = "̔"
IOTA_SUBSCRIPT = "ͅ"
GREEK_VOWELS = "αεηιουω"


def greek_transliteration(surface: str) -> str:
    """Romanize an accented Greek word.

    MACULA Greek publishes no transliteration column, so unlike Hebrew this is
    always derived. Accents and the smooth breathing are dropped; the rough
    breathing becomes an `h` written before the whole vowel it opens, and an
    iota subscript is written on the line.
    """
    if not surface:
        return ""
    letters: list[tuple[str, bool, bool]] = []
    for character in unicodedata.normalize("NFD", surface.lower()):
        if unicodedata.combining(character):
            if not letters:
                continue
            base, rough, subscript = letters[-1]
            letters[-1] = (
                base,
                rough or character == ROUGH_BREATHING,
                subscript or character == IOTA_SUBSCRIPT,
            )
            continue
        letters.append((character, False, False))

    pieces: list[str] = []
    index = 0
    while index < len(letters):
        character, rough, subscript = letters[index]
        following = letters[index + 1][0] if index + 1 < len(letters) else ""
        pair = character + following
        if character == "ρ":
            pieces.append("rh" if rough else "r")
            index += 1
            continue
        if pair in GREEK_DIPHTHONGS:
            # A breathing belongs to the diphthong as a whole but is printed on
            # its second vowel, so both letters are inspected before deciding.
            _, second_rough, second_subscript = letters[index + 1]
            pieces.append(("h" if rough or second_rough else "") + GREEK_DIPHTHONGS[pair])
            if second_subscript:
                pieces.append("i")
            index += 2
            continue
        if character == "γ" and following in "γκξχ":
            pieces.append("n")
            index += 1
            continue
        if character in GREEK_VOWELS and rough:
            pieces.append("h")
        pieces.append(GREEK_LETTERS.get(character, character))
        if subscript:
            pieces.append("i")
        index += 1
    result = "".join(pieces)
    # Greek marks a proper name with a capital, and so should its romanization.
    return result[:1].upper() + result[1:] if surface[:1].isupper() else result
