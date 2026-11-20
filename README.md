# bookkeeper

Collects a quarter's purchase invoices and receipts, sorts each one into a VAT treatment, checks it against the bank
statements and shows the figures of the Malta VAT return (VAT FORM 003/2011) for you to check.

It was written for one Maltese sole trader with purchases only. It **never files or submits anything** and nothing
in it is tax advice: you decide every tax question and you submit the return yourself.

## What it does

- Searches Gmail (read-only scope) for invoices and receipts and downloads the attachments and hosted PDFs.
- Adds documents you collect by hand, including phone photos of receipts.
- Reads the facts of each document (type, net, VAT, gross, rate, VAT numbers), with a regex pass and optionally an AI model.
- Builds one table with a treatment per document: domestic input VAT, reverse charge (EU / non-EU), OSS risk, blocked,
  no tax invoice, personal, duplicate, and so on. Amounts are converted to EUR with the ECB reference rate and supplier
  VAT numbers are checked in VIES.
- Matches the documents with bank statement CSV exports and lists payments that have no document.
- Writes `summary.csv`, a worksheet, and two merged PDFs (claimed documents, everything else).
- Serves a local portal (127.0.0.1 only) to upload files, answer the open questions and see the return form.

## Setup

```bash
conda env create -f environment.yml
conda activate bookkeeper
cp config.example.yml config.yml
```

1. Put your Gmail addresses in `config.yml`.
2. Create an OAuth desktop client in a Google Cloud project with the Gmail API enabled and save it as `credentials.json`
   next to the script. The first run opens the browser once per mailbox.
3. Optional: to let an AI model read documents the regex pass cannot, set `ai_model` in `config.yml` and the
   `ANTHROPIC_API_KEY` environment variable.
4. Set your registration date and rates in `rules.yml`, and your own decisions in `output/decisions.yml`:

```yaml
vat_number: MT00000000
suppliers:
  - {match: 'acme hosting', business: true, country: DE}
documents:
  INV-1039: personal
ignore_payments: ['rent', 'transfer to savings']
```

## Use

```bash
python bookkeeper.py --dry-run             # list the candidate emails of the last completed quarter
python bookkeeper.py --quarter 2026-Q3     # download and build a quarter
python bookkeeper.py --portal              # the local portal on http://127.0.0.1:8765
python -m unittest                         # tests, on made-up documents
```

Hand-collected invoices and receipt photos go under `output/expenses/`, bank statement CSV exports under
`output/statements/`; both can also be added in the portal.

## What stays private

`config.yml`, `credentials.json`, the tokens, `.env` and the whole `output/` folder (documents, ledger, decisions,
statements, cache with full email content) are gitignored. Only document text is sent to the AI provider, and only
when you configure one.

## Limits

- Malta only; purchases only (reverse-charge services and services / overheads at 18%). Sales, goods for re-sale,
  capital goods and other rates are not calculated.
- Bank statements are read from CSV exports; there is no bank API and no PDF statement reader.
- Built-in handling exists for Bolt Business ride reports; other suppliers go through the general rules.
