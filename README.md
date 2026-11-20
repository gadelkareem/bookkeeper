# bookkeeper

Collects a quarter's purchase invoices and receipts, adds the invoices you issued, sorts each one into a VAT treatment,
checks it against the bank statements and shows the figures of the Malta VAT return (VAT FORM 003/2011) for you to check.

It was written for one Maltese sole trader. It **never files or submits anything** and nothing
in it is tax advice: you decide every tax question and you submit the return yourself.

## What it does

- Searches Gmail (read-only scope) for invoices and receipts and downloads the attachments and hosted PDFs.
- Adds documents you collect by hand, including phone photos of receipts.
- Reads the facts of each document (type, net, VAT, gross, rate, VAT numbers), with a regex pass and optionally an AI model.
- Builds one table with a treatment per document: domestic input VAT, reverse charge (EU / non-EU), OSS risk, blocked,
  no tax invoice, personal, duplicate, and so on. Amounts are converted to EUR with the ECB reference rate and supplier
  VAT numbers are checked in VIES.
- Reads the invoices you issued from a CSV list and / or the PDFs (`output/sales/`) and puts each in its box: taxable
  sales per rate, VAT-registered EU customers (with the recapitulative statement), customers outside the EU, exports,
  exempt. It reports invoice numbers missing from the series.
- Matches the documents with bank statement CSV exports: money out against purchases, money in against sales. It lists
  payments without a document and money received without a sales invoice.
- Writes `summary.csv`, a worksheet, and merged PDFs (claimed documents, everything else, sales).
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
4. Check the rates in `rules.yml`, and put your own facts and decisions in `output/decisions.yml`:

```yaml
vat_number: MT00000000
registration_date: 2026-01-01
suppliers:
  - {match: 'acme hosting', business: true, country: DE}
documents:
  INV-1039: personal
ignore_payments: ['rent', 'transfer to savings']
credit_brought_forward: {2026-Q4: 0}    # Box 44; without it, Box 42 of the return filed for the quarter before
```

## Try it with made-up data

```bash
python demo.py
python bookkeeper.py --portal --out demo_output --expenses demo_output/expenses
```

`demo.py` writes an invented quarter (purchases of every treatment, a sales list, two sales PDFs, one bank export) and
runs the normal build on it. No mailbox, AI key or real document is needed.

## Use

```bash
python bookkeeper.py --dry-run             # list the candidate emails of the last completed quarter
python bookkeeper.py --quarter 2026-Q3     # download and build a quarter
python bookkeeper.py --portal              # the local portal on http://127.0.0.1:8765
python -m unittest                         # tests, on made-up documents
```

Hand-collected invoices and receipt photos go under `output/expenses/`, bank statement CSV exports under
`output/statements/`, and the invoices you issued under `output/sales/`; all three can also be added in the portal.

The sales list is a CSV with a header row; columns are found by name: invoice number, date, customer, country,
customer VAT (number), net, VAT, total, VAT rate, currency, supply (goods or services). A PDF with the same invoice
number is attached to its line. Without a list, the PDFs are read by the AI.

## What stays private

`config.yml`, `credentials.json`, the tokens, `.env` and the whole `output/` folder (documents, ledger, decisions,
statements, cache with full email content) are gitignored. Only document text is sent to the AI provider, and only
when you configure one.

## Limits

- Malta only. Purchases: reverse-charge services and services / overheads at 18%. Sales: Boxes 1, 2 and 18 to 26.
  Goods for re-sale, capital goods, purchases at other rates and adjustments are not calculated.
- The box a sale goes in is a proposal from the customer's country, VAT number and the VAT charged. Place-of-supply
  rules have exceptions the tool does not know; unclear cases are left for you in the review queue.
- Bank statements are read from CSV exports; there is no bank API and no PDF statement reader.
- Built-in handling exists for Bolt Business ride reports; other suppliers go through the general rules.

## Licence

MIT, see [LICENSE](LICENSE).
