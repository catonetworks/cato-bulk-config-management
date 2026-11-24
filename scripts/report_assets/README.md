# Report assets

Brand assets embedded in the customer readiness report that
`scripts/preflight_check.py` writes (`preflight_report.html`, and `.pdf` with `--pdf`).

| File | Source | Notes |
|---|---|---|
| `cato_logo_rgb_color_art_101.svg` | Cato Brand Hub logo kit | Official artwork, unmodified. SHA-256 `63d48dab...578b5` |
| `cato_logo_rgb_white_art_101.svg` | Cato Brand Hub logo kit | Official artwork, unmodified. SHA-256 `bb0ba252...134eb` |
| `archivo-latin-wght-normal.woff2` | Archivo (Omnibus-Type) | SIL Open Font License 1.1, see `Archivo-OFL.txt` |
| `Inter-Regular.woff`, `Inter-Bold.woff` | Inter (rsms) | SIL Open Font License 1.1, see `Inter-OFL.txt` |

Brand rules followed by the report: Cato Green #128461 leads, secondary colors only
carry meaning (Coral for items required before migration, Sunflower for items to
confirm), the green gradient (125deg, #128461 to #00392F) for the cover band,
Archivo Medium for headlines and Inter for everything else, the flat 2D Ring for
the readiness gauge, outline icons on a 24 px grid with a 2 px round stroke, US
English and sentence-case headings. Do not redraw, recolor or edit the logo files;
the renderer checks their hashes and leaves the logo out if they change.
