# Source attribution

This project aligns the public-domain American Standard Version with documented
Greek, Hebrew, and Aramaic source projections. The alignments select,
normalize, segment, and restructure fields from the sources below; they are not
unaltered copies of any one upstream dataset.

## Reusable attribution statement

The following block may be copied into a redistribution or derivative work:

> This work contains American Standard Version text, which is in the public
> domain, and Westminster Leningrad Codex biblical text, which is in the public
> domain. Its original-language alignments are derived from the MACULA Greek
> and MACULA Hebrew Linguistic Datasets (© 2022–2024 Biblica, Inc.), the Open
> Scriptures Hebrew Bible project, the SBL Greek New Testament (© 2010 Society
> of Biblical Literature and Logos Bible Software), and J. J. McCollum's
> SBLGNT-TEI transcription and apparatus encoding. Hebrew syntax fields include
> data from the Westminster Hebrew Syntax without Morphology (© 1991–2018 J.
> Alan Groves Center for Advanced Biblical Research), and Hebrew glosses include
> data by Andi Wu (© 2022 Cherith Analytics). Greek gloss fields draw on the
> public-domain Berean Interlinear Bible and, where identified by MACULA, Cherith
> gloss data by Andi Wu (© 2023 Cherith Analytics). The credited datasets and
> annotations are licensed under the Creative Commons Attribution 4.0
> International License except where identified as public domain. Source fields
> were selected, normalized, segmented, and restructured to create the
> alignments. See
> https://creativecommons.org/licenses/by/4.0/.

Equivalent wording is fine as long as it preserves the source credits, license
identification, and indication that the material was changed.

## Pinned sources and component notes

- **American Standard Version.** Public-domain English text, obtained from
  [openbibleinfo/American-Standard-Version-Bible](https://github.com/openbibleinfo/American-Standard-Version-Bible/tree/5c83ee265c75b3b1c056435eff622a875f1edc45).
- **MACULA Greek Linguistic Datasets.** © 2022–2024 Biblica, Inc.; CC BY 4.0.
  Pinned source:
  [Clear-Bible/macula-greek](https://github.com/Clear-Bible/macula-greek/tree/8423afe47b9e8f24b7772e808af45c7159a6fe7e).
  This project selects and transforms linguistic fields rather than
  redistributing the vendor checkout.
- **MACULA Hebrew Linguistic Datasets.** © 2022–2024 Biblica, Inc.; CC BY 4.0.
  Pinned source:
  [Clear-Bible/macula-hebrew](https://github.com/Clear-Bible/macula-hebrew/tree/47db250bd55d0d8577f2a94fba114ef16c35b23c).
  The embedded WLC biblical text is public domain; linguistic annotations and
  credited component data retain their upstream terms.
- **Westminster Hebrew Syntax without Morphology.** © 1991–2018 J. Alan Groves
  Center for Advanced Biblical Research; CC BY 4.0. Syntax-derived fields are
  distributed through the pinned MACULA Hebrew data.
- **Hebrew Cherith glosses.** Andi Wu, © 2022 Cherith Analytics; CC BY 4.0.
  These glosses are distributed through the pinned MACULA Hebrew data.
- **Greek gloss components.** MACULA identifies its `gloss` field with the
  public-domain Berean Interlinear Bible and its Cherith gloss data with Andi
  Wu, © 2023 Cherith Analytics; CC BY 4.0.
- **Open Scriptures Hebrew Bible.** Lemma, morphology, and related annotation
  data are CC BY 4.0 and should be credited to the Open Scriptures Hebrew Bible
  project. The WLC biblical text is public domain. Pinned source:
  [openscriptures/morphhb](https://github.com/openscriptures/morphhb/tree/3d15126fb1ef74867fc1434be1942e837932691f).
- **SBL Greek New Testament and apparatus.** © 2010 Society of Biblical
  Literature and Logos Bible Software; CC BY 4.0. Pinned source:
  [Faithlife/SBLGNT](https://github.com/Faithlife/SBLGNT/tree/c4d241a9c1c479a55b989ba35a4976c1d0b8052c).
- **SBLGNT-TEI transcription and apparatus encoding.** J. J. McCollum; CC BY
  4.0. Pinned source:
  [jjmccollum/sblgnt-tei](https://github.com/jjmccollum/sblgnt-tei/tree/2a95589b2a9cb9b8bda5372650c7f55745ceecad).

The exact repository URLs, commits, and sparse source paths used by the build
are recorded in `config/sources.lock.json`. Vendored source repositories are
not included in this publication.
