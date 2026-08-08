# Third-Party Notices

## Pixiv-Shaft synonym dictionary

`tag_aliases.tsv` contains a selected and modified subset derived from the Google Play variant
of the Pixiv-Shaft built-in synonym dictionary:

- Project: https://github.com/CeuiLiSA/Pixiv-Shaft
- Source file: `app/src/google/assets/synonym_dict_builtin.json`
- Pinned commit: `0281abe3864612ecb88aac3df3ce0f87c531bd38`
- Upstream copyright: Copyright (c) 2021 CeuiLiSA
- License: MIT; see `third_party/Pixiv-Shaft-LICENSE.txt`

Modifications made by this project include selecting common non-adult visual attributes, removing
overly broad aliases, resolving targets to reviewed Pixiv search tags, adding a small number of
manually reviewed Chinese/English aliases, normalizing duplicates, and converting the result to
two-column UTF-8 TSV.

Pixiv tag names and translations may originate from Pixiv users and remain subject to applicable
rights and platform terms. This notice does not claim ownership of Pixiv content or trademarks.
