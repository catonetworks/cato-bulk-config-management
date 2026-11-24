#!/usr/bin/env python3
"""Write the Cato-branded one-page Getting Started Guide to Account Migration.

The guide is generic (any pair of accounts): the API keys to gather, the commands
that run the before-you-migrate check, and what to do with its report. It uses the
same official logo, fonts and palette as the readiness report (scripts/report_assets/),
and prints to a single A4 or Letter page.

Usage:
  python3 scripts/getting_started.py                       # docs/getting-started-guide.html
  python3 scripts/getting_started.py --pdf                 # ... and the PDF beside it
  python3 scripts/getting_started.py --out /tmp/guide.html --pdf
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from preflight_report import COPY_JS, _esc, _font_face, _logo, chain, html_to_pdf  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE = "Getting Started Guide to Account Migration"

_I = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
ICON_EYE = f'<svg viewBox="0 0 24 24" {_I}><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>'
ICON_EDIT = f'<svg viewBox="0 0 24 24" {_I}><path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg>'
ICON_REPORT = f'<svg viewBox="0 0 24 24" {_I}><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></svg>'
ICON_REPEAT = f'<svg viewBox="0 0 24 24" {_I}><path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5"/></svg>'
ICON_RUN = f'<svg viewBox="0 0 24 24" {_I}><path d="M5 4l14 8-14 8z"/></svg>'

# Each block is one chain: every line but the last ends in "&&". Pasted with its line breaks, the shell
# carries on to the next line after "&&"; pasted by a PDF viewer that turns line breaks into spaces, it is
# the same chain on one line. So no command may be split over two lines (a " \\" continuation only works
# when the line break survives): every line is kept short enough to print on one line (LINE_MAX).
LINE_MAX = 96
ONCE_PER_COMPUTER = [
    "git clone https://github.com/catonetworks/cato-bulk-config-management.git",
    "cd cato-bulk-config-management",
    "python3 -m venv .venv && .venv/bin/pip install catocli",
    'export PATH="$PWD/.venv/bin:$PATH"',
]
ONCE_PER_PAIR = [
    "catocli configure set --profile source --cato-token 'CURRENT_KEY' --account-id CURRENT_NUMBER",
    "catocli configure set --profile target --cato-token 'NEW_KEY' --account-id NEW_NUMBER",
]
# export_source.py runs catocli's export, the static hosts on every site type and the webhooks, and carries on
# past any type the key is refused for lack of permission (recorded for the check to report).
EXPORT = ["python3 scripts/export_source.py --profile source"]
CHECK = "python3 scripts/preflight_check.py --source-profile source --target-profile target --probe --pdf"


def block(lines, sep="&&"):
    """The text of one block, one command per line (see preflight_report.chain)."""
    return chain(lines, sep)


# The export block runs every command even if the first is partly refused (a key whose role lacks one policy
# permission): "&&" would skip the static hosts and webhooks too.
SEPARATOR = {"EXPORT": ";"}


# Print sizes for code: Chrome-printed PDFs split "_" onto its own line when copied at some sizes
# (9.6px and 8pt do; 9 to 13px in half-pixel steps do not). Check with a PDF text extract after changing them.
CSS = """
:root{--green:#128461;--moss:#006F53;--forest:#003E34;--pine:#002C28;--hl:#00FFCC;--pale:#F0F0F0;--mid:#646464;--dark:#464646}
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;background:#fff;color:#1b1b1b;font:14px/1.5 Inter,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.sheet{max-width:860px;margin:0 auto}
h1,h2{font-family:Archivo,Inter,sans-serif;font-weight:500;letter-spacing:-.01em;margin:0}
.hero{background:linear-gradient(125deg,#128461,#00392F);color:#fff;padding:26px 34px 28px;border-radius:0 0 18px 18px}
.hero .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}
.hero .logo img{height:28px;width:auto;display:block}
.eyebrow{font-size:11.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.hero .eyebrow{color:var(--hl)}
.hero h1{font-size:34px;line-height:1.12;margin:8px 0 10px}
.hero p{margin:0;color:#dff5ec;font-size:14.5px;max-width:640px}
.tag{font-size:12px;font-weight:700;color:var(--forest);background:#fff;border-radius:999px;padding:5px 12px}
main{padding:6px 34px 0}
.step{display:grid;grid-template-columns:40px 1fr;gap:14px;padding:18px 0 4px;break-inside:avoid}
.disc{width:36px;height:36px;border-radius:50%;background:var(--green);color:#fff;font:500 18px Archivo,Inter,sans-serif;
display:flex;align-items:center;justify-content:center}
h2{font-size:21px;color:var(--pine);margin:4px 0 8px}
p{margin:0 0 8px}
.keys{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:2px 0 8px}
.key{border:1px solid #dfe7e4;border-radius:12px;padding:10px 14px;display:grid;grid-template-columns:28px 1fr;gap:10px;align-items:start}
.key svg,.next svg{width:24px;height:24px;color:var(--green)}
.key b{display:block;color:var(--pine);font-size:14.5px}
.key .perm{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--moss)}
.key span{font-size:13px;color:#333}
.small{font-size:12.5px;color:var(--dark)}
.small code,.inl{font:12px ui-monospace,Menlo,Consolas,monospace;background:var(--pale);padding:1px 5px;border-radius:4px}
.cap{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--moss);margin:8px 0 3px}
.cmd{position:relative;background:var(--pine);border-radius:9px;margin:0 0 4px}
.cmd pre{margin:0;padding:7px 72px 7px 12px;color:#e8fff7;font:11.5px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre;overflow-x:auto}
.cmd button{position:absolute;top:6px;right:7px;font:600 11.5px Inter,sans-serif;color:var(--pine);background:var(--hl);border:0;border-radius:6px;padding:3px 9px;cursor:pointer}
.main-cmd{border:2px solid var(--green);border-radius:12px;padding:10px 12px 8px;margin:10px 0 6px;background:#f2faf6}
.main-cmd .cap{margin-top:0}
.next{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:4px 0 6px}
.next div{border-top:3px solid var(--green);padding:8px 2px 0}
.next b{display:block;color:var(--pine);margin:4px 0 2px}
.next span{font-size:13px;color:#333}
footer{display:flex;gap:16px;align-items:center;margin:14px 34px 0;padding:12px 0 18px;border-top:1px solid #e3e3e3;font-size:11.5px;color:var(--mid)}
footer img{height:18px;width:auto;flex:none}
@page{size:A4;margin:0}
@media print{body{font-size:12px;line-height:1.45}.hero{border-radius:0;padding:16px 28px 18px}.hero .top{margin-bottom:12px}
.hero h1{font-size:26px;margin:6px 0 6px}.hero p{font-size:12.5px}main{padding:0 28px}.step{padding-top:8px}h2{font-size:17px;margin:2px 0 5px}
.keys{margin-bottom:6px}.main-cmd{margin:6px 0 2px;padding:7px 10px 5px}.next{margin-bottom:2px}.next div{padding-top:5px}
.cmd button{display:none}.cmd pre{padding-right:12px;font-size:10px;line-height:1.45;white-space:pre-wrap}.cap{margin-top:6px}.key{padding:8px 12px}.key span,.next span{font-size:12px}
.small{font-size:11.5px}footer{margin:10px 28px 0;padding:8px 0 0}}
"""


def _cmds(lines, sep="&&"):
    """One panel per block (no comment lines, which zsh rejects when pasted)."""
    return f'<div class="cmd"><button type="button">Copy</button><pre>{_esc(block(lines, sep))}</pre></div>'


def render_html():
    return f"""<!DOCTYPE html><html lang="en-US"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(TITLE)}</title>
<!-- Fonts: Archivo and Inter, SIL Open Font License 1.1. Logo: official Cato artwork, unmodified. -->
<style>{_font_face()}{CSS}</style></head><body><div class="sheet">
<header class="hero"><div class="top"><div class="logo">{_logo('cato_logo_rgb_white_art_101.svg')}</div>
<span class="tag">Three steps</span></div>
<div class="eyebrow">Account migration</div><h1>{_esc(TITLE)}</h1>
<p>Find out what a new Cato account needs before anything moves. Nothing here changes either account's live configuration.</p>
</header>
<main>
<section class="step"><div class="disc">1</div><div>
<h2>Gather two API keys</h2>
<div class="keys">
<div class="key">{ICON_EYE}<div><div class="perm">View</div><b>Current account</b><span>The one you're moving from. A view key can only read, so this account stays safe.</span></div></div>
<div class="key">{ICON_EDIT}<div><div class="perm">Edit</div><b>New account</b><span>The one you're moving to. The check tests objects in a draft it deletes straight away.</span></div></div>
</div>
<p class="small">Create each key in that account's Cato Management Application (CMA) under Resources &gt; API Keys
(Admin API Keys in newer versions), signed in as an admin whose role covers the whole account (Viewer or Editor), and
copy it at once: it's shown only once. A narrower role can't read every policy: the export carries on without
those, and the check names each permission the role is missing. Note each account number too: it's the number
after <code>/account/</code> in the CMA's address bar.</p>
</div></section>

<section class="step"><div class="disc">2</div><div>
<h2>Run the check</h2>
<p class="small">Open Terminal (you need git and Python 3). Replace the words in CAPITALS with your own values, keep the
single quotes around each key, and paste each block whole: it runs as one.</p>
<div class="cap">Once per computer</div>{_cmds(ONCE_PER_COMPUTER)}
<div class="cap">Once per pair of accounts</div>{_cmds(ONCE_PER_PAIR)}
<div class="cap">Export the current account (read-only, a few minutes)</div>{_cmds(EXPORT, SEPARATOR["EXPORT"])}
<div class="main-cmd"><div class="cap">The check: the one to remember, and to run again after every fix</div>{_cmds([CHECK])}</div>
</div></section>

<section class="step"><div class="disc">3</div><div>
<h2>What to do next</h2>
<div class="next">
<div>{ICON_REPORT}<b>Open the report</b><span>Open <span class="inl">preflight_report.html</span> in the <span class="inl">preflight</span> folder, then send the PDF beside it to everyone its action plan names.</span></div>
<div>{ICON_REPEAT}<b>Check again</b><span>Run the check again as items get done, until nothing says "Required before migration."</span></div>
<div>{ICON_RUN}<b>Run the migration</b><span>Follow Part 2 of the report: every Terraform command, in order, filled in for your two accounts.</span></div>
</div>
</div></section>
</main>
<footer>{_logo('cato_logo_rgb_color_art_101.svg')}</footer>
</div>{COPY_JS}</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "docs", "getting-started-guide.html"),
                    help="where to write the HTML (default: docs/getting-started-guide.html)")
    ap.add_argument("--pdf", action="store_true", help="also print it to a PDF beside the HTML (needs Chrome, Chromium or Edge)")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(render_html())
    print(f"wrote {args.out}")
    if args.pdf:
        pdf = os.path.splitext(args.out)[0] + ".pdf"
        err = html_to_pdf(args.out, pdf)
        print(f"PDF not written: {err}" if err else f"wrote {pdf}")
        return 1 if err else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
