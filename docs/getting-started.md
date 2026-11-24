# Getting Started Guide to Account Migration

Three steps to find out what a new Cato account needs before anything moves.
Nothing here changes either account's live configuration.
A Cato-branded, one-page copy to print or send: [getting-started-guide.pdf](getting-started-guide.pdf)
(rebuild it with `python3 scripts/getting_started.py --pdf`).

## 1. Gather two API keys

| Account | Key | Why |
|---|---|---|
| Current account (the one you're moving from) | View | A view key can only read, so this account stays safe. |
| New account (the one you're moving to) | Edit | The check tests objects in a draft it deletes straight away. |

Create each key in that account's Cato Management Application (CMA) under
Resources > API Keys (Admin API Keys in newer versions), signed in as an admin
whose role covers the whole account (Viewer or Editor), and copy it at once:
it's shown only once. A narrower role can't read every policy: the export carries
on without those, and the check names each permission the role is missing. Note
each account number too: it's the number after `/account/` in the CMA's address bar.

## 2. Run the check

Open Terminal (you need git and Python 3). Replace the words in CAPITALS with
your own values, keep the single quotes around each key, and paste each block whole: it runs as one.

Once per computer:

```bash
git clone https://github.com/catonetworks/cato-bulk-config-management.git &&
cd cato-bulk-config-management &&
python3 -m venv .venv && .venv/bin/pip install catocli &&
export PATH="$PWD/.venv/bin:$PATH"
```

Once per pair of accounts:

```bash
catocli configure set --profile source --cato-token 'CURRENT_KEY' --account-id CURRENT_NUMBER &&
catocli configure set --profile target --cato-token 'NEW_KEY' --account-id NEW_NUMBER
```

Export the current account (read-only, a few minutes; it carries on past anything the key isn't allowed to read):

```bash
python3 scripts/export_source.py --profile source
```

The check: the one to remember, and to run again after every fix:

```bash
python3 scripts/preflight_check.py --source-profile source --target-profile target --probe --pdf
```

## 3. What to do next

1. **Open the report:** `preflight/preflight_report.html`, then send the PDF beside it to everyone its action plan names.
2. **Check again** as items get done, until nothing says "Required before migration."
3. **Run the migration** from Part 2 of the report: every Terraform command, in order, filled in for your two accounts.

Every finding is explained in [preflight-check.md](preflight-check.md), and Appendix B of the report has the technical notes.
