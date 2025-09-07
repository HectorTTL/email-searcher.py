# email-searcher.py
A CLI tool to search .eml email exports by keyword.
It uses ripgrep/grep to pre-filter candidates and a Python verifier that:

ignores base64 and HTML bodies (to avoid false positives),

extracts and shows the Date,

flags attachments as BIJLAGE (via Content-Disposition: attachment or name=),

prints inbox/outbox paths in different colors, and date color by age (bright <1y, yellow 1–2y, magenta >2y).
Includes a spinner/status line so you see work immediately and a single-line progress bar during scanning.

Usage

python3 email-searcher.py -t "STRING"             # case-insensitive, obvious age colors (default)
python3 email-searcher.py -t "STRING" -cs         # case-sensitive
python3 email-searcher.py -t "STRING" -txt        # also write results to ./output.txt
python3 email-searcher.py -t "STRING" -j1         # single-thread verify (quieter output)
python3 email-searcher.py -t "STRING" --fade-age  # use subtle greys instead of obvious colors


Dependencies

Python 3.8+

Optional but recommended: ripgrep (rg) for best speed; otherwise falls back to GNU grep.
