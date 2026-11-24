# Bundled Japanese font

`NotoSansJP-Bold.otf` is **Noto Sans CJK JP Bold** (Adobe/Google, SIL Open Font
License 1.1, see `NotoSansJP-OFL.txt`), subset to the JIS X 0208 character set
(the 7,572 glyphs a Japanese caption or title needs: kana, kanji, ASCII, the
punctuation and full-width forms) with `fontTools`:

```
pyftsubset NotoSansCJKjp-Bold.otf \
  --output-file=NotoSansJP-Bold.otf \
  --text-file=jp_chars.txt --layout-features='*' --no-hinting --desubroutinize
```

It is in the repository because `telop.py` needs kana and kanji on the GitHub
runner, and the workflow file may not be edited to install a font. `libass` is
pointed at this folder with `fontsdir=` on the `ass` filter, and the family name
in the ASS styles is `Noto Sans CJK JP`. To refresh it, download the upstream
`NotoSansCJKjp-Bold.otf` from the noto-cjk repository and re-run the command
above; keep the licence file beside it.
