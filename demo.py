"""A made-up quarter to try bookkeeper.py without a mailbox, an AI key or real documents.

    python demo.py                 # writes demo_output/ and builds the last completed quarter
    python bookkeeper.py --portal --out demo_output --expenses demo_output/expenses

Every company, person, VAT number and amount here is invented. Purchases are written straight into the ledger with
their facts (as the AI would have read them), sales come from a CSV list plus two invoice PDFs, and one bank export
pays most of them. The data is built to show every treatment and a few open questions, not a tidy quarter.
"""
import csv
import json
import os
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault('BOOKKEEPER_NO_VIES', '1')  # invented VAT numbers: never ask VIES; the ones below are pre-filled in the cache
import bookkeeper as b  # noqa: E402

OWNER_VAT = 'MT10000001'
# vendor, supplier country, VAT number, VAT country, rate, currency, doc type, category, shows the buyer VAT number, business
SUPPLIERS = [
    ('Harbour Telecom Ltd', 'MT', 'MT20000011', 'MT', 18, 'EUR', 'tax_invoice', 'telecom', True, True),
    ('Gozo Office Supplies', 'MT', 'MT20000022', 'MT', 18, 'EUR', 'tax_invoice', 'hardware', True, True),
    ('Valletta Coworking Ltd', 'MT', 'MT20000033', 'MT', 18, 'EUR', 'tax_invoice', 'rent', False, True),
    ('Nordwolke Hosting GmbH', 'DE', 'DE200000044', None, 0, 'EUR', 'tax_invoice', 'hosting', True, True),
    ('Tulp Design Tools BV', 'NL', 'NL200000055B01', None, 0, 'EUR', 'tax_invoice', 'software', True, True),
    ('Pixelforge Inc', 'US', '', None, 0, 'USD', 'tax_invoice', 'software', False, True),
    ('Streamly Sarl', 'LU', 'LU20000066', 'MT', 18, 'EUR', 'simplified_invoice', 'software', False, True),
    ('Amstel Parts BV', 'NL', 'NL200000077B01', 'NL', 21, 'EUR', 'tax_invoice', 'hardware', False, False),
    ('Sliema Corner Cafe', 'MT', 'MT20000088', 'MT', 18, 'EUR', 'fiscal_receipt', 'food_drink', False, False),
    ('Zipcab Malta', 'MT', 'MT20000099', 'MT', 18, 'EUR', 'tax_invoice', 'transport', False, False),
    ('Marsa Print Shop', 'MT', 'MT20000110', 'MT', 18, 'EUR', 'tax_invoice', 'professional_services', False, False),
]
# customer, country, VAT number, VAT rate (None = no VAT charged), currency, goods or services
CUSTOMERS = [
    ('Mdina Boutique Hotel Ltd', 'MT', 'MT30000011', 18, 'EUR', 'services'),
    ('Birgu Sailing Club', 'MT', '', 18, 'EUR', 'services'),
    ('Rheinwerk Medien GmbH', 'DE', 'DE300000022', None, 'EUR', 'services'),
    ('Kanaal Studio BV', 'NL', 'NL300000033B01', None, 'EUR', 'services'),
    ('Lakeshore Analytics LLC', 'US', '', None, 'USD', 'services'),
    ('Luca Bianchi', 'IT', '', None, 'EUR', 'services'),  # a consumer in the EU without VAT charged: an open question
]


def pdf(path: Path, lines: list[str]) -> None:
    """A one-page PDF with the given lines (a picture of text: enough for a file link and the merged packets)."""
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (900, 1200), 'white')
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((60, 60 + 34 * i), line, fill='black')
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, 'PDF')


def build(out: Path, label: str | None = None, seed: int = 7) -> tuple[Path, str]:
    """Write the made-up quarter under `out` and run the normal build on it. Returns (quarter folder, label)."""
    rnd = random.Random(seed)
    label = label or b.last_quarter()
    start, end = b.quarter_range(label)
    days = (end - start).days
    if out.exists():
        if not (out / '.demo').exists():
            sys.exit(f'{out} exists and was not written by demo.py: choose another folder')
        shutil.rmtree(out)
    qdir = out / label
    qdir.mkdir(parents=True)
    (out / '.demo').write_text('made-up data, written by demo.py\n')
    (out / 'decisions.yml').write_text(
        f'# Made-up decisions for the demo quarter.\nvat_number: {OWNER_VAT}\nregistration_date: {start.replace(month=1, day=1)}\nsuppliers:\n'
        + ''.join(f"  - {{match: '{name.split()[0].lower()}', business: true}}\n" for name, *rest in SUPPLIERS if rest[-1])
        + "  - {match: 'zipcab', treatment: personal, reason: 'taxi ride, no business purpose recorded', review: 'taxi ride: business purpose?'}\n"
        + "ignore_payments: ['rent', 'own savings']\n")

    ledger, bank, day_of = {}, [], lambda: start + timedelta(days=rnd.randrange(2, days - 2))
    pay = lambda day, amount, text: bank.append((day + timedelta(days=rnd.randrange(0, 6)), amount, text))
    n = 0
    for vendor, sc, vat_no, vc, rate, cur, dt, cat, shows_buyer, _ in SUPPLIERS:
        for _ in range(rnd.choice((1, 2, 3))):
            n += 1
            day, net = day_of(), round(rnd.uniform(12, 480), 2)
            vat = round(net * rate / 100, 2)
            no = f'{vendor[:2].upper()}-{rnd.randrange(10000, 99999)}'
            rel = f'{b.slug(vendor)}/{day}_{no}.pdf'
            pdf(qdir / rel, [vendor, f'VAT no. {vat_no}' if vat_no else '', '', f'{dt.replace("_", " ").upper()} {no}', f'Date: {day}',
                             f'Bill to: Demo Trader{", VAT " + OWNER_VAT if shows_buyer else ""}', '', f'Net: {cur} {net:.2f}',
                             f'VAT {rate}%: {cur} {vat:.2f}' if rate else 'Reverse charge: VAT to be accounted for by the customer', f'Total: {cur} {net + vat:.2f}'])
            ledger[f'demo{n:03d}'] = {
                'is_financial_document': True, 'doc_type': dt, 'document_type': 'invoice', 'vendor': vendor, 'date': day.isoformat(), 'invoice_number': no,
                'currency': cur, 'total_amount': round(net + vat, 2), 'net_amount': net, 'vat_amount': vat, 'vat_rate': rate, 'vat_country': vc,
                'supplier_country': sc, 'supplier_vat': vat_no or None, 'buyer_vat': OWNER_VAT if shows_buyer else None, 'reverse_charge': not rate,
                'supply': 'goods' if cat == 'hardware' and sc != 'MT' and rate else 'services', 'category': cat, 'confidence': 0.95, 'facts_v': b.FACTS_V,
                'files': [rel], 'folder': label, 'source': 'email', 'ai': True, 'subject': f'Your invoice {no}', 'links': []}
            if rnd.random() < 0.85:  # most are paid from the account; USD at about the ECB rate
                pay(day, -round((net + vat) / (1.08 if cur == 'USD' else 1), 2), vendor.upper()[:18])
    first = next(k for k, r in ledger.items() if r['doc_type'] == 'tax_invoice')  # the order confirmation of one invoice: a duplicate
    ledger['demo900'] = {**ledger[first], 'doc_type': 'order_confirmation', 'files': [], 'subject': 'Order confirmed'}
    ledger['demo901'] = {**ledger[first], 'vendor': 'Unknown Web Shop', 'invoice_number': 'W-55120', 'supplier_country': None, 'supplier_vat': None,
                         'vat_country': None, 'buyer_vat': None, 'category': 'other', 'confidence': 0.55, 'files': [], 'total_amount': 64.9, 'net_amount': None, 'vat_amount': None, 'vat_rate': None}
    (out / 'ledger.json').write_text(json.dumps(ledger, indent=2))

    sales, no = [], 40
    for customer, cc, vat_no, rate, cur, supply in CUSTOMERS:
        for _ in range(rnd.choice((1, 2, 3))):
            no += 1 if no != 46 else 2  # INV-0047 is left out on purpose: the gap check finds it
            day, net = day_of(), round(rnd.uniform(300, 4200), 2)
            vat = round(net * (rate or 0) / 100, 2)
            sales.append([f'INV-{no:04d}', day.isoformat(), customer, cc, vat_no, f'{net:.2f}', f'{vat:.2f}', f'{net + vat:.2f}', rate or 0, cur, supply])
            if rnd.random() < 0.75:
                pay(day + timedelta(days=10), round((net + vat) / (1.08 if cur == 'USD' else 1), 2), f'TRANSFER FROM {customer.upper()[:20]}')
    credit = sales[0]  # a credit note for part of the first invoice
    sales.append([f'INV-{no + 1:04d}', (end - timedelta(days=3)).isoformat(), credit[2], credit[3], credit[4], '-100.00', '-18.00', '-118.00', 18, 'EUR', 'services'])
    (out / 'sales').mkdir()
    with open(out / 'sales' / 'invoices_issued.csv', 'w', newline='') as f:
        csv.writer(f).writerows([['Invoice number', 'Invoice date', 'Customer', 'Country', 'Customer VAT', 'Net', 'VAT', 'Total', 'VAT rate', 'Currency', 'Supply']] + sales)
    for row in sales[:2]:  # the PDFs of two of them: the list gives the figures, the file is attached
        pdf(out / 'sales' / f'{row[0]}.pdf', ['Demo Trader', f'VAT no. {OWNER_VAT}', '', f'INVOICE {row[0]}', f'Date: {row[1]}', f'Bill to: {row[2]} ({row[3]})',
                                              '', f'Net: {row[9]} {row[5]}', f'VAT {row[8]}%: {row[9]} {row[6]}', f'Total: {row[9]} {row[7]}'])
        ledger_sale = {'vendor': row[2], 'date': row[1], 'invoice_number': row[0], 'currency': row[9], 'total_amount': float(row[7]), 'confidence': 0.9, 'source': 'sales', 'files': []}
        ledger[f'sale:{row[0]}.pdf'] = ledger_sale
    (out / 'ledger.json').write_text(json.dumps(ledger, indent=2))

    bank += [(start + timedelta(days=20), -45.0, 'SKYLINE PARKING'), (start + timedelta(days=33), -700.0, 'RENT AUGUST'),
             (start + timedelta(days=40), 250.0, 'TRANSFER FROM J BORG'), (start + timedelta(days=41), -1000.0, 'TO OWN SAVINGS')]
    (out / 'statements').mkdir()
    with open(out / 'statements' / 'demo_bank.csv', 'w', newline='') as f:
        csv.writer(f).writerows([['Date', 'Description', 'Amount', 'Currency']] + [[d.isoformat(), text, f'{amount:.2f}', 'EUR'] for d, amount, text in sorted(bank)])

    (out / '_cache').mkdir()
    today = date.today().isoformat()
    numbers = [v for _, _, v, *_ in SUPPLIERS if v] + [v for _, _, v, *_ in CUSTOMERS if v]
    (out / '_cache' / 'vies.json').write_text(json.dumps({v: {'valid': True, 'checked': today} for v in numbers}))
    (out / '_cache' / 'ecb_rates.json').write_text(json.dumps({(start - timedelta(days=1)).isoformat(): {'USD': 1.08, 'GBP': 0.86}}))
    b.run_final(out, label, None, out / 'expenses')
    return qdir, label


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else b.HERE / 'demo_output'
    folder, quarter = build(target)
    form = b.return_form([{**r, **{k: float(r[k]) if r[k] else None for k in ('net_eur', 'vat_eur', 'rc_vat_eur', 'rate_pct')}} for r in csv.DictReader(open(folder / 'summary.csv'))])
    print(f'\nMade-up quarter {quarter} built in {folder}')
    print(f'  sales:     Box 1 {form["1"]:.2f}   Box 2 {form["2"]:.2f}   taxable {form["22"]:.2f}   output VAT (Box 25) {form["25"]:.2f}')
    print(f'  purchases: reverse charge {form["5"]:.2f} / {form["8"]:.2f}   domestic {form["31"]:.2f} / {form["37"]:.2f}')
    print(f'  result:    excess credit (Box 42) {form["42"]:.2f}   tax due (Box 43) {form["43"]:.2f}')
    print(f'Open it:  python bookkeeper.py --portal --quarter {quarter} --out {target} --expenses {target / "expenses"}')
