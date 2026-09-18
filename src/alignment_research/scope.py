from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookSpec:
    code: str
    name: str
    testament: str
    number: str
    usx_file: str
    macula_code: str
    osis_name: str
    # New Testament coordinates; absent for an Old Testament book.
    macula_lowfat_file: str | None = None
    tei_id: str | None = None
    sblgnt_name: str | None = None
    sblgnt_apparatus_name: str | None = None
    # Old Testament coordinates; absent for a New Testament book.
    macula_hebrew_stem: str | None = None
    oshb_file: str | None = None


# Every per-book coordinate the pipeline needs, in canonical order. A book
# belongs to exactly one testament and carries only that testament's source
# columns, because the two halves are aligned against different editions:
# SBLGNT with its apparatus for the New Testament, and the Westminster
# Leningrad Codex with its ketiv/qere for the Old.
#
# For the New Testament the position in _NT_ROWS is also the MACULA lowfat file
# number and the TEI division number, so both are derived rather than repeated.
# Each column was verified against the pinned sources: the ASV USX and MACULA
# lowfat file names from their repository trees, the MACULA book codes from the
# SBLGNT TSV, the TEI divisions from their Greek incipits, and both SBLGNT
# names from the edition and apparatus files themselves. The two SBLGNT names
# differ for most books: the edition file uses
# its own stem as the reference prefix while the apparatus spells the book out.
#
# OSIS book names are retained for reader exports that use OSIS references.
_NT_ROWS = (
    # code, name, number, macula, sblgnt text, sblgnt apparatus, OSIS, lowfat
    ('MAT', 'Matthew', '40', 'MAT', 'Matt', 'Matthew', 'Matt', 'matthew'),
    ('MRK', 'Mark', '41', 'MRK', 'Mark', 'Mark', 'Mark', 'mark'),
    ('LUK', 'Luke', '42', 'LUK', 'Luke', 'Luke', 'Luke', 'luke'),
    ('JHN', 'John', '43', 'JHN', 'John', 'John', 'John', 'john'),
    ('ACT', 'Acts', '44', 'ACT', 'Acts', 'Acts', 'Acts', 'acts'),
    ('ROM', 'Romans', '45', 'ROM', 'Rom', 'Romans', 'Rom', 'romans'),
    ('1CO', '1 Corinthians', '46', '1CO', '1Cor', '1 Corinthians', '1Cor', '1corinthians'),
    ('2CO', '2 Corinthians', '47', '2CO', '2Cor', '2 Corinthians', '2Cor', '2corinthians'),
    ('GAL', 'Galatians', '48', 'GAL', 'Gal', 'Galatians', 'Gal', 'galatians'),
    ('EPH', 'Ephesians', '49', 'EPH', 'Eph', 'Ephesians', 'Eph', 'ephesians'),
    ('PHP', 'Philippians', '50', 'PHP', 'Phil', 'Philippians', 'Phil', 'philippians'),
    ('COL', 'Colossians', '51', 'COL', 'Col', 'Colossians', 'Col', 'colossians'),
    ('1TH', '1 Thessalonians', '52', '1TH', '1Thess', '1 Thessalonians', '1Thess', '1thessalonians'),
    ('2TH', '2 Thessalonians', '53', '2TH', '2Thess', '2 Thessalonians', '2Thess', '2thessalonians'),
    ('1TI', '1 Timothy', '54', '1TI', '1Tim', '1 Timothy', '1Tim', '1timothy'),
    ('2TI', '2 Timothy', '55', '2TI', '2Tim', '2 Timothy', '2Tim', '2timothy'),
    ('TIT', 'Titus', '56', 'TIT', 'Titus', 'Titus', 'Titus', 'titus'),
    ('PHM', 'Philemon', '57', 'PHM', 'Phlm', 'Philemon', 'Phlm', 'philemon'),
    ('HEB', 'Hebrews', '58', 'HEB', 'Heb', 'Hebrews', 'Heb', 'hebrews'),
    ('JAS', 'James', '59', 'JAS', 'Jas', 'James', 'Jas', 'james'),
    ('1PE', '1 Peter', '60', '1PE', '1Pet', '1 Peter', '1Pet', '1peter'),
    ('2PE', '2 Peter', '61', '2PE', '2Pet', '2 Peter', '2Pet', '2peter'),
    ('1JN', '1 John', '62', '1JN', '1John', '1 John', '1John', '1john'),
    ('2JN', '2 John', '63', '2JN', '2John', '2 John', '2John', '2john'),
    ('3JN', '3 John', '64', '3JN', '3John', '3 John', '3John', '3john'),
    ('JUD', 'Jude', '65', 'JUD', 'Jude', 'Jude', 'Jude', 'jude'),
    ('REV', 'Revelation', '66', 'REV', 'Rev', 'Revelation', 'Rev', 'revelation'),
)


# MACULA Hebrew ships one lowfat file per chapter rather than per book, so the
# stem here is completed with the chapter number by ChapterScope. The stems
# follow the ASV USX numbering and codes, with one upstream irregularity:
# Hosea's files are spelled `28-HOS`, not `28-Hos`. MACULA's in-file book code
# is the USX code itself, which the build re-checks against the fetched
# chapter. OSHB uses its own OSIS-style file names, so those need their own
# column.
_OT_ROWS = (
    # code, name, number, macula lowfat stem, oshb file, OSIS
    ('GEN', 'Genesis', '01', '01-Gen', 'Gen', 'Gen'),
    ('EXO', 'Exodus', '02', '02-Exo', 'Exod', 'Exod'),
    ('LEV', 'Leviticus', '03', '03-Lev', 'Lev', 'Lev'),
    ('NUM', 'Numbers', '04', '04-Num', 'Num', 'Num'),
    ('DEU', 'Deuteronomy', '05', '05-Deu', 'Deut', 'Deut'),
    ('JOS', 'Joshua', '06', '06-Jos', 'Josh', 'Josh'),
    ('JDG', 'Judges', '07', '07-Jdg', 'Judg', 'Judg'),
    ('RUT', 'Ruth', '08', '08-Rut', 'Ruth', 'Ruth'),
    ('1SA', '1 Samuel', '09', '09-1Sa', '1Sam', '1Sam'),
    ('2SA', '2 Samuel', '10', '10-2Sa', '2Sam', '2Sam'),
    ('1KI', '1 Kings', '11', '11-1Ki', '1Kgs', '1Kgs'),
    ('2KI', '2 Kings', '12', '12-2Ki', '2Kgs', '2Kgs'),
    ('1CH', '1 Chronicles', '13', '13-1Ch', '1Chr', '1Chr'),
    ('2CH', '2 Chronicles', '14', '14-2Ch', '2Chr', '2Chr'),
    ('EZR', 'Ezra', '15', '15-Ezr', 'Ezra', 'Ezra'),
    ('NEH', 'Nehemiah', '16', '16-Neh', 'Neh', 'Neh'),
    ('EST', 'Esther', '17', '17-Est', 'Esth', 'Esth'),
    ('JOB', 'Job', '18', '18-Job', 'Job', 'Job'),
    ('PSA', 'Psalms', '19', '19-Psa', 'Ps', 'Ps'),
    ('PRO', 'Proverbs', '20', '20-Pro', 'Prov', 'Prov'),
    ('ECC', 'Ecclesiastes', '21', '21-Ecc', 'Eccl', 'Eccl'),
    ('SNG', 'Song of Solomon', '22', '22-Sng', 'Song', 'Song'),
    ('ISA', 'Isaiah', '23', '23-Isa', 'Isa', 'Isa'),
    ('JER', 'Jeremiah', '24', '24-Jer', 'Jer', 'Jer'),
    ('LAM', 'Lamentations', '25', '25-Lam', 'Lam', 'Lam'),
    ('EZK', 'Ezekiel', '26', '26-Ezk', 'Ezek', 'Ezek'),
    ('DAN', 'Daniel', '27', '27-Dan', 'Dan', 'Dan'),
    ('HOS', 'Hosea', '28', '28-HOS', 'Hos', 'Hos'),
    ('JOL', 'Joel', '29', '29-Jol', 'Joel', 'Joel'),
    ('AMO', 'Amos', '30', '30-Amo', 'Amos', 'Amos'),
    ('OBA', 'Obadiah', '31', '31-Oba', 'Obad', 'Obad'),
    ('JON', 'Jonah', '32', '32-Jon', 'Jonah', 'Jonah'),
    ('MIC', 'Micah', '33', '33-Mic', 'Mic', 'Mic'),
    ('NAM', 'Nahum', '34', '34-Nam', 'Nah', 'Nah'),
    ('HAB', 'Habakkuk', '35', '35-Hab', 'Hab', 'Hab'),
    ('ZEP', 'Zephaniah', '36', '36-Zep', 'Zeph', 'Zeph'),
    ('HAG', 'Haggai', '37', '37-Hag', 'Hag', 'Hag'),
    ('ZEC', 'Zechariah', '38', '38-Zec', 'Zech', 'Zech'),
    ('MAL', 'Malachi', '39', '39-Mal', 'Mal', 'Mal'),
)


BOOKS: dict[str, BookSpec] = {
    row[0]: BookSpec(
        code=row[0],
        name=row[1],
        testament="OT",
        number=row[2],
        usx_file=f"{row[2]}-{row[0]}.usx",
        macula_code=row[0],
        osis_name=row[5],
        macula_hebrew_stem=row[3],
        oshb_file=f"{row[4]}.xml",
    )
    for row in _OT_ROWS
}

BOOKS.update({
    row[0]: BookSpec(
        code=row[0],
        name=row[1],
        testament="NT",
        number=row[2],
        usx_file=f"{row[2]}-{row[0]}.usx",
        macula_code=row[3],
        osis_name=row[6],
        macula_lowfat_file=f"{position:02d}-{row[7]}.xml",
        tei_id=f"B{position:02d}",
        sblgnt_name=row[4],
        sblgnt_apparatus_name=row[5],
    )
    for position, row in enumerate(_NT_ROWS, 1)
})


# What each testament's source side is, for display and for the language tags
# that reach the finished USX and its HTML. Daniel and Ezra also contain
# Aramaic, which MACULA marks per token, so a token may override the chapter
# default.
SOURCE_LANGUAGES = {
    "OT": {"code": "hbo", "name": "Hebrew", "direction": "rtl", "edition": "WLC"},
    "NT": {"code": "grc", "name": "Greek", "direction": "ltr", "edition": "SBLGNT"},
}


@dataclass(frozen=True)
class ChapterScope:
    book: BookSpec
    chapter: int

    @property
    def ref(self) -> str:
        return f"{self.book.code}.{self.chapter}"

    @property
    def slug(self) -> str:
        return self.ref

    @property
    def chapter_name(self) -> str:
        """How the chapter is cited in prose, for pages people read.

        The book names are plural where the book is a collection, which reads
        wrong for one of its chapters: a page of Psalm 1 is not a page of
        Psalms 1.
        """
        singular = {"PSA": "Psalm"}.get(self.book.code, self.book.name)
        return f"{singular} {self.chapter}"

    @property
    def lock_suffix(self) -> str:
        return self.ref

    @property
    def variant_decisions_name(self) -> str:
        return f"{self.slug}-variant-decisions.json"

    @property
    def verse_divisions_name(self) -> str:
        """Where one Hebrew verse divides into two ASV verses, when it does.

        Almost no chapter has one — five in the whole Hebrew Bible — so this
        file is absent for nearly every chapter, and its absence is a fact the
        build checks rather than a gap it works around.
        """
        return f"{self.slug}-verse-divisions.json"

    @property
    def unsourced_verses_name(self) -> str:
        """How an ASV verse the pinned edition does not contain is resolved.

        Rarer even than a verse division — Nehemiah 7 is the only chapter in
        the Bible with one — so this file is absent for every other chapter,
        and its absence is checked rather than assumed.
        """
        return f"{self.slug}-unsourced-verses.json"

    @property
    def testament(self) -> str:
        return self.book.testament

    @property
    def is_hebrew(self) -> bool:
        return self.book.testament == "OT"

    @property
    def source_language(self) -> dict[str, str]:
        return SOURCE_LANGUAGES[self.book.testament]

    def macula_hebrew_lowfat_file_for(self, chapter: int) -> str:
        """MACULA Hebrew is published one lowfat file per chapter.

        The chapter is a parameter because an ASV chapter is not always filled
        by the Hebrew chapter of the same number alone: Isaiah 64:1 is written
        in the Hebrew Isaiah 63, so building Isaiah 64 reads both files.
        """
        if not self.is_hebrew:
            raise ValueError(f"{self.ref} is not an Old Testament chapter")
        return f"{self.book.macula_hebrew_stem}-{chapter:03d}-lowfat.xml"

    @property
    def macula_hebrew_lowfat_file(self) -> str:
        """This chapter's own lowfat file."""
        return self.macula_hebrew_lowfat_file_for(self.chapter)


def chapter_scope(ref: str) -> ChapterScope:
    pieces = ref.upper().split(".")
    if len(pieces) != 2 or pieces[0] not in BOOKS:
        supported = ", ".join(sorted(BOOKS))
        raise ValueError(f"unsupported chapter {ref!r}; supported book codes: {supported}")
    try:
        chapter = int(pieces[1])
    except ValueError as error:
        raise ValueError(f"invalid chapter reference {ref!r}") from error
    if chapter < 1:
        raise ValueError(f"invalid chapter reference {ref!r}")
    return ChapterScope(BOOKS[pieces[0]], chapter)
