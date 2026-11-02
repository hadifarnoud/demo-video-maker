# Right-to-left and Persian videos

Checked on the Persian Praxabase tour. The engine handles subtitles, cards and speech-to-text folding.

## Script

- Persian runs about 25 % longer than English. Budget for it and tighten lines. `voice.speed` barely changes eleven_v4.
- `«»` means "label on screen". Do not use it for ordinary quotes. Avoid a suffix glued to a label («تنها دارنده»ی).
- Avoid calques: a noun used as a verb (چک‌لیست می‌کنند), "comes back as" (برمی‌گردد). Have a native-level reviewer read it.
- Quote digits as rendered. Apps mix Latin and Persian digits across pages; respell them to words for the voice.
- Expert quotes can stay colloquial in the script. Listen to confirm the voice kept them colloquial.

## Voice

- Fix ambiguous vowels with marks in the review page: خِبْره, کویرْ فِرِیت, ezafe with kasra (جواب‌هایِ خودِ).
- Brand names: test spellings with short takes. Praxabase needed «پْرَکسا بِیس»; the plain script spelling and Latin both failed.
- A slash is read aloud ("اسلش"): respell the label with a pause.
- A dash after a label gives a pause where the next verb would run into it.
- Speech-to-text writes loanwords its own way (دماراژ) and normalizes colloquial speech, and its output varies between runs. Treat it as a guide; only a person can confirm.

## Screen

- A site's landing page may have no Persian version. Then find where a Persian visitor really meets that action (a signup page, signed out, with the language cookie set) rather than mixing languages on screen.
- Forms that autofocus show a caret and focus ring: blur them when nothing is typed.
- Labels render with «», “” or straight quotes depending on the page; the recorder tolerates all three.
