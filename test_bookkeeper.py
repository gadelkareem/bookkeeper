"""Tests for the VAT table. Run: python -m unittest -v   (conda env bookkeeper)

The rule tests use made-up documents. The golden test compares a real quarter with figures the owner filed:
it reads output/golden.json (not committed) and is skipped when that file or the quarter's summary.csv is missing.
"""
import csv
import json
import os
import unittest
import warnings
from pathlib import Path

warnings.simplefilter('ignore')
os.environ['BOOKKEEPER_NO_VIES'] = '1'
import bookkeeper as b  # noqa: E402

RULES = {'home_country': 'MT', 'standard_rate': 18, 'registration_date': '2026-04-01', 'min_confidence': 0.7, 'documents': {},
         'suppliers': [{'match': 'acme|cloudco|usai|ossco', 'business': True}, {'match': r'\bcab\b', 'treatment': 'personal', 'review': 'business purpose?'}]}


class Fx:
    def rate(self, cur, day):
        return ({'USD': 1.1448, 'GBP': 0.8575}.get(cur), day)


def doc(**kw):
    base = {'vendor': 'Acme Ltd', 'date': '2026-08-01', 'doc_type': 'tax_invoice', 'invoice_number': 'A-100001', 'currency': 'EUR',
            'total_amount': 118.0, 'net_amount': 100.0, 'vat_amount': 18.0, 'vat_rate': 18, 'vat_country': 'MT', 'supplier_country': 'MT',
            'supplier_vat': 'MT12345678', 'buyer_vat': 'MT00000000', 'supply': 'services', 'confidence': 0.95, 'files': ['Acme/a.pdf'], 'facts_v': b.FACTS_V}
    return {**base, **kw}


def row(vendors=None, rules=RULES, **kw):
    return b.build_row('k1', doc(**kw), rules, vendors or {}, Fx())


class Amounts(unittest.TestCase):
    def test_clean_invoice(self):
        r = row()
        self.assertEqual((r['treatment'], r['net_eur'], r['vat_eur'], r['gross_eur'], r['needs_review']), ('domestic_input', 100.0, 18.0, 118.0, False))

    def test_total_recorded_as_net_is_repaired(self):  # a net price recorded as the total
        r = row(total_amount=25.42, net_amount=25.42, vat_amount=4.57)
        self.assertEqual((r['net'], r['vat'], r['gross']), (25.42, 4.57, 29.99))
        self.assertTrue(r['needs_review'])

    def test_rate_read_as_amount(self):  # "7.000%" read as VAT 7.00 on a 15.00 total
        r = row(total_amount=15.0, net_amount=None, vat_amount=7.0, vat_rate=7, supplier_country='MT', vat_country='MT')
        self.assertEqual(r['vat'], 0.98)
        self.assertIn('impossible', r['review_reason'])

    def test_net_price_read_as_vat(self):
        r = row(total_amount=16.8, net_amount=None, vat_amount=10.2, vat_rate=18)
        self.assertEqual(r['vat'], 2.56)

    def test_vat_from_rate_when_missing(self):
        r = row(total_amount=118.0, net_amount=None, vat_amount=None)
        self.assertEqual((r['net'], r['vat']), (100.0, 18.0))

    def test_net_plus_vat_mismatch_is_flagged(self):
        self.assertIn('!= gross', row(total_amount=130.0)['review_reason'])

    def test_usd_at_ecb_rate(self):
        r = row(vendor='USAI Inc', currency='USD', total_amount=20.0, net_amount=20.0, vat_amount=0, vat_rate=0, vat_country=None, supplier_country='US')
        self.assertEqual((r['treatment'], r['net_eur'], r['rc_vat_eur'], r['fx_rate']), ('reverse_charge_non_eu', 17.47, 3.14, 1.1448))

    def test_eur_vat_stated_on_foreign_invoice(self):  # GBP invoice that prints the Maltese VAT in EUR
        r = row(vendor='OssCo LLC', currency='GBP', total_amount=29.99, net_amount=25.42, vat_amount=4.57, vat_amount_eur=5.33, supplier_country='US', buyer_vat=None)
        self.assertEqual((r['treatment'], r['net_eur'], r['vat_eur']), ('oss_risk', 29.61, 5.33))


class Treatments(unittest.TestCase):
    def t(self, **kw):
        return row(**kw)['treatment']

    def test_order_of_rules(self):
        self.assertEqual(self.t(doc_type='fee_schedule'), 'not_a_purchase')
        self.assertEqual(self.t(total_amount=0.0, net_amount=0.0, vat_amount=0.0), 'not_a_purchase')
        self.assertEqual(self.t(date='2026-03-31'), 'pre_registration')
        self.assertEqual(self.t(duplicate_of='k0'), 'duplicate')
        for d in ('quotation', 'order_confirmation', 'payment_receipt'):
            self.assertEqual(self.t(doc_type=d), 'no_tax_invoice')
        self.assertEqual(self.t(doc_type='fiscal_receipt'), 'no_tax_invoice')
        self.assertEqual(self.t(vendor='City Cab'), 'personal')
        self.assertEqual(self.t(vat_country='NL', vat_rate=21, total_amount=121.0, vat_amount=21.0, supplier_country='IE'), 'foreign_vat')
        self.assertEqual(self.t(vat_country='EG', vat_rate=14, total_amount=114.0, vat_amount=14.0, supplier_country='EG'), 'foreign_vat')
        self.assertEqual(self.t(vendor='OssCo', supplier_country='LU', buyer_vat=None), 'oss_risk')
        rates = {**RULES, 'vat_rates': {'MT': [18, 5], 'LU': [17], 'NL': [21]}}  # 18% labelled LU is Maltese VAT; 21% labelled NL stays Dutch
        self.assertEqual(row(rules=rates, vendor='OssCo', supplier_country='LU', vat_country='LU', buyer_vat=None)['treatment'], 'oss_risk')
        self.assertEqual(row(rules=rates, vendor='OssCo', supplier_country='IE', vat_country='NL', vat_rate=21, total_amount=121.0, vat_amount=21.0)['treatment'], 'foreign_vat')
        self.assertEqual(self.t(vendor='Diner', category='food_drink'), 'blocked')
        self.assertEqual(self.t(total_amount=900.0, net_amount=900.0, vat_amount=0.0, vat_rate=0, vat_country=None), 'exempt_or_zero')

    def test_reverse_charge(self):
        no_vat = dict(total_amount=100.0, net_amount=100.0, vat_amount=0, vat_rate=0, vat_country=None)
        eu = row(vendor='CloudCo GmbH', supplier_country='DE', **no_vat)
        self.assertEqual((eu['treatment'], eu['rc_vat_eur'], eu['vat_eur']), ('reverse_charge_eu', 18.0, 0))
        self.assertEqual(self.t(vendor='USAI', supplier_country='US', buyer_vat=None, **no_vat), 'reverse_charge_non_eu')
        # unknown foreign supplier, no buyer VAT number: a candidate for the owner, never in the boxes
        self.assertEqual(self.t(vendor='Some App', supplier_country='US', buyer_vat=None, **no_vat), 'needs_review')
        self.assertEqual(self.t(vendor='CloudCo GmbH', supplier_country='CN', supply='goods', **no_vat), 'needs_review')

    def test_business_use_needs_a_signal(self):
        self.assertEqual(self.t(vendor='Unknown Shop', buyer_vat=None), 'needs_review')
        self.assertEqual(self.t(vendor='Unknown Shop', buyer_vat='MT1'), 'domestic_input')
        self.assertEqual(row(vendors={'Unknown Shop': {'deductible': False, 'reason': 'x'}}, vendor='Unknown Shop')['treatment'], 'personal')
        ai = row(vendors={'Unknown Shop': {'deductible': True}}, vendor='Unknown Shop', buyer_vat=None)
        self.assertEqual(ai['treatment'], 'domestic_input')
        self.assertIn('AI guess', ai['review_reason'])

    def test_one_label_per_supplier(self):  # the facts and the supplier rule decide, not a per-document opinion
        rows = [row(vendor='USAI', supplier_country='US', total_amount=18.0, net_amount=18.0, vat_amount=v, vat_rate=None, vat_country=None, confidence=c)
                for v, c in ((None, 0.65), (None, 0.85), (0, 0.95))]
        self.assertEqual({r['treatment'] for r in rows}, {'reverse_charge_non_eu'})

    def test_owner_decision_wins(self):
        rules = {**RULES, 'documents': {'a100001': {'treatment': 'personal', 'note': 'private'}}}
        self.assertEqual(row(rules=rules)['treatment'], 'personal')
        self.assertEqual(row(rules=rules, duplicate_of='k0')['treatment'], 'duplicate')

    def test_buyer_vat_must_be_the_owners(self):
        mine = {**RULES, 'vat_number': 'mt00000000'}
        self.assertEqual(row(rules=mine, vendor='Unknown Shop')['treatment'], 'domestic_input')
        self.assertEqual(row(rules=mine, vendor='Unknown Shop', buyer_vat='MT 99999999')['treatment'], 'needs_review')
        self.assertEqual(row(vendor='Unknown Shop', buyer_vat='MT12345678')['treatment'], 'needs_review')  # the supplier's own number

    def test_owner_checked_row_leaves_the_queue(self):
        rules = {**RULES, 'documents': {'k1': {'accepted': True, 'note': 'office printer'}}}
        r = row(rules=rules, buyer_vat=None)
        self.assertEqual((r['treatment'], r['needs_review']), ('domestic_input', False))
        self.assertIn('office printer', r['reason'])
        claim = row(rules={**RULES, 'documents': {'k1': {'treatment': 'domestic_input', 'accepted': True}}}, vendor='City Cab')
        self.assertEqual((claim['treatment'], claim['needs_review']), ('domestic_input', False))

    def test_needs_review_is_derived(self):
        self.assertTrue(row(confidence=0.15)['needs_review'])
        self.assertTrue(row(buyer_vat=None)['needs_review'])  # buyer VAT number missing: a risk item
        self.assertTrue(row(files=[])['needs_review'])


class Duplicates(unittest.TestCase):
    def dup(self, *docs):
        keys = {id(d): f'k{i}' for i, d in enumerate(docs)}
        b.mark_duplicates(list(docs), keys)
        return [d['duplicate_of'] for d in docs]

    def test_same_invoice_number(self):
        self.assertEqual(self.dup(doc(), doc(files=['x.png'])), [None, 'k0'])

    def test_order_confirmation_and_invoice(self):
        got = self.dup(doc(), doc(doc_type='order_confirmation', invoice_number='ORD-5', date='2026-08-03', vat_amount=None))
        self.assertEqual(got, [None, 'k0'])

    def test_same_number_under_another_sender(self):
        self.assertEqual(self.dup(doc(invoice_number='INV-1039'), doc(vendor='Jane Doe', invoice_number='INv-1039', total_amount=100.0, files=['x.png'])), [None, 'k0'])

    def test_two_rides_same_price_are_not_duplicates(self):
        a = doc(vendor='City Cab', invoice_number=None, total_amount=7.9, vat_amount=1.21)
        self.assertEqual(self.dup(a, doc(vendor='City Cab', invoice_number=None, total_amount=7.9, vat_amount=0.0)), [None, None])

    def test_two_invoices_same_amount_are_not_duplicates(self):
        self.assertEqual(self.dup(doc(), doc(invoice_number='A-100002')), [None, None])

    def test_same_amount_other_supplier_is_only_noted(self):
        a, c = doc(), doc(vendor='PayCo', invoice_number='TX9', doc_type='payment_receipt')
        self.assertEqual(self.dup(a, c), [None, None])
        self.assertEqual(c.get('possible_duplicate_of'), 'k0')


class Table(unittest.TestCase):
    def setUp(self):
        no_vat = dict(total_amount=100.0, net_amount=100.0, vat_amount=0, vat_rate=0, vat_country=None)
        docs = [doc(), doc(invoice_number='A-100002', total_amount=21.0, net_amount=17.8, vat_amount=3.2),
                doc(vendor='OssCo', invoice_number='O-1', supplier_country='IE', total_amount=9.99, net_amount=8.47, vat_amount=1.52),
                doc(vendor='CloudCo GmbH', invoice_number='C-1', supplier_country='DE', **no_vat),
                doc(vendor='USAI', invoice_number='U-1', supplier_country='US', currency='USD', **{**no_vat, 'total_amount': 20.0, 'net_amount': 20.0}),
                doc(duplicate_of='k0'), doc(vendor='Diner', invoice_number='D-1', category='food_drink')]
        self.rows = [b.build_row(f'k{i}', d, RULES, {}, Fx()) for i, d in enumerate(docs)]

    def test_boxes(self):
        x = b.return_boxes(self.rows)
        self.assertEqual((x['3'], x['6'], x['4'], x['7'], x['5'], x['8'], x['17']), (100.0, 18.0, 17.47, 3.14, 117.47, 21.14, 0.0))
        self.assertEqual((x['31'], x['37'], x['31_without_oss'], x['37_without_oss'], x['42']), (126.27, 22.72, 117.8, 21.2, 22.72))

    def test_checks_pass_and_catch_a_double_count(self):
        self.assertTrue(all(ok for _, ok, _ in b.table_checks(self.rows, '2026-Q3')))
        twice = self.rows + [b.build_row('k9', doc(), RULES, {}, Fx())]
        self.assertFalse(dict((n, ok) for n, ok, _ in b.table_checks(twice, '2026-Q3'))['no document counted twice'])
        self.assertFalse(dict((n, ok) for n, ok, _ in b.table_checks(self.rows, '2026-Q4'))['every claimed row is dated inside the period'])

    def test_sums_per_treatment_equal_the_rows(self):
        for r in self.rows:
            if None not in (r['net'], r['vat'], r['gross']):
                self.assertAlmostEqual(r['net'] + r['vat'], r['gross'], places=2)
        self.assertEqual(sum(r['treatment'] == 'duplicate' for r in self.rows), 1)


class Bank(unittest.TestCase):
    def test_reads_revolut_and_debit_credit_layouts(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / 'revolut.csv').write_text('Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n'
                                       'CARD_PAYMENT,Current,2026-08-01 10:00:00,2026-08-02 09:00:00,Acme Ltd,-118.00,0.00,EUR,COMPLETED,10\n'
                                       'CARD_PAYMENT,Current,2026-08-03 10:00:00,,Shop,-5.00,0.00,EUR,REVERTED,10\n')
        (d / 'bank.csv').write_text('Account statement\n\nDate;Details;Debit;Credit\n05/08/2026;USAI INC;17,60;\n06/08/2026;Salary;;"1.000,00"\n')
        txs = b.read_statements(d)
        self.assertEqual([(t['date'], t['amount'], t['account']) for t in txs],
                         [('2026-08-05', -17.6, 'bank'), ('2026-08-06', 1000.0, 'bank'), ('2026-08-02', -118.0, 'revolut')])

    def test_matching(self):
        fx = Fx()
        rows = [b.build_row('k0', doc(), RULES, {}, fx),
                b.build_row('k1', doc(vendor='USAI Inc', invoice_number='U-1', supplier_country='US', currency='USD', total_amount=20.0, net_amount=20.0,
                                       vat_amount=0, vat_rate=0, vat_country=None), RULES, {}, fx),
                b.build_row('k2', doc(invoice_number='A-100002', total_amount=59.0, net_amount=50.0, vat_amount=9.0), RULES, {}, fx)]
        tx = lambda day, amount, text: {'date': day, 'amount': amount, 'currency': 'EUR', 'text': text, 'account': 'acc', 'used': False}
        txs = [tx('2026-08-02', -118.0, 'ACME LTD'), tx('2026-08-03', -118.0, 'Someone else'), tx('2026-08-04', -17.6, 'USAI'),
               tx('2026-08-20', -300.0, 'Mystery'), tx('2026-08-21', -900.0, 'Rent transfer'), tx('2026-08-05', 500.0, 'income')]
        left = b.match_payments(rows, txs, {'ignore_payments': ['rent']})
        self.assertTrue(rows[0]['payment'].startswith('2026-08-02 acc EUR 118.00'))
        self.assertIn('other currency', rows[1]['payment'])
        self.assertEqual(rows[2]['payment'], 'no payment found')
        self.assertIn('no payment found', rows[2]['review_reason'])
        self.assertEqual([t['text'] for t in left], ['Mystery', 'Someone else'])

    def test_no_statement_is_not_a_missing_payment(self):
        r = b.build_row('k0', doc(date='2026-09-20'), RULES, {}, Fx())
        b.match_payments([r], [{'date': '2026-08-02', 'amount': -1.0, 'currency': 'EUR', 'text': 'x', 'account': 'a', 'used': False}])
        self.assertEqual((r['payment'], r['needs_review']), ('no statement for this date', False))

    def test_owner_links_a_payment_to_a_document(self):
        r = b.build_row('k0', doc(date='2026-08-01'), RULES, {}, Fx())
        t = {'date': '2026-08-09', 'amount': -55.0, 'currency': 'EUR', 'text': 'Unrelated name', 'account': 'a', 'used': False}
        left = b.match_payments([r], [t], {'payment_links': {b.payment_id(t): 'k0'}})
        self.assertIn('linked by you', r['payment'])
        self.assertEqual(left, [])

    def test_tags_say_where_a_document_belongs(self):
        r = b.build_row('k0', doc(date='2026-08-01'), RULES, {}, Fx())
        r.update(treatment='reverse_charge_eu', payment='no payment found', needs_review=True, source='manual', source_file='x/a.jpg',
                 review_reason='taxi ride: business purpose?; no payment found in the statements: cash or another card?; something new')
        tags, flags = b.row_tags(r)
        self.assertEqual((tags[0], tags[-2:]), ('Box 3/6', ['receipt photo', 'no payment']))
        self.assertEqual(flags, ['taxi', 'no payment', 'other'])


class Photos(unittest.TestCase):
    def setUp(self):
        import tempfile
        from PIL import Image
        self.d = Path(tempfile.mkdtemp())
        Image.frombytes('RGB', (3000, 2000), os.urandom(3000 * 2000 * 3)).save(self.d / 'big.png')  # noise: does not compress

    def test_large_photo_is_scaled(self):
        from io import BytesIO
        from PIL import Image
        ext, data = b.manual_bytes(self.d / 'big.png')
        self.assertEqual((ext, max(Image.open(BytesIO(data)).size)), ('.jpg', b.PHOTO_MAX_PX))

    def test_heic_becomes_jpeg(self):
        import shutil
        import subprocess
        if not shutil.which('sips'):
            self.skipTest('sips (macOS) not available')
        subprocess.run(['sips', '-Z', '400', '-s', 'format', 'heic', str(self.d / 'big.png'), '--out', str(self.d / 'p.heic')], capture_output=True, check=True)
        ext, data = b.manual_bytes(self.d / 'p.heic')
        self.assertEqual((ext, data[:2]), ('.jpg', b'\xff\xd8'))

    def test_photo_read_claim_is_flagged(self):
        self.assertIn('photo', row(files=['Acme/a.jpg'])['review_reason'])


class Sales(unittest.TestCase):
    """The invoices the owner issued: treatment, boxes, the sales list, payments received."""

    def sale(self, **kw):
        base = {'vendor': 'Hotel Ltd', 'date': '2026-08-01', 'invoice_number': 'INV-0001', 'currency': 'EUR', 'net_amount': 1000.0, 'vat_amount': 180.0,
                'total_amount': 1180.0, 'vat_rate': 18, 'customer_country': 'MT', 'customer_vat': '', 'supply': 'services', 'confidence': 1.0, 'files': []}
        return b.sale_row(kw.pop('key', 's1'), {**base, **kw}, kw.pop('rules', RULES), Fx())

    def no_vat(self, **kw):
        return self.sale(vat_amount=0, total_amount=1000.0, vat_rate=0, **kw)

    def test_where_a_sale_is_reported(self):
        self.assertEqual(self.sale()['treatment'], 'sale_domestic')
        self.assertEqual(self.no_vat(customer_country='DE', customer_vat='DE 123 456 789')['treatment'], 'sale_eu_reverse_charge')
        self.assertEqual(self.no_vat(customer_country='', customer_vat='NL123456789B01')['treatment'], 'sale_eu_reverse_charge')  # country from the VAT number
        self.assertEqual(self.no_vat(customer_country='US')['treatment'], 'sale_outside_malta')
        self.assertEqual(self.no_vat(customer_country='US', supply='goods')['treatment'], 'sale_export')
        self.assertEqual(self.no_vat(treatment_hint='sale_exempt')['treatment'], 'sale_exempt')

    def test_unclear_sales_go_to_review(self):
        for kw in ({'customer_country': 'IT'}, {'customer_country': 'MT'}, {'customer_country': ''}):  # EU consumer, Maltese without VAT, unknown
            r = self.no_vat(**kw)
            self.assertEqual((r['treatment'], r['needs_review']), ('needs_review', True), kw)
        self.assertEqual(self.sale(vat_amount=210.0, total_amount=1210.0, vat_rate=21)['treatment'], 'needs_review')  # not a rate of the form
        self.assertIn('!= gross', self.sale(total_amount=1200.0)['review_reason'])
        self.assertEqual(self.sale(date='2026-03-01')['treatment'], 'pre_registration')

    def test_amounts_are_completed_and_converted(self):
        r = self.sale(vat_amount=None, total_amount=None)  # only net and rate in the list
        self.assertEqual((r['vat'], r['gross']), (180.0, 1180.0))
        r = self.no_vat(customer_country='US', currency='USD')
        self.assertEqual((r['net_eur'], r['fx_rate']), (873.52, 1.1448))

    def test_owner_decision_and_purchase_rules_do_not_mix(self):
        rules = {**RULES, 'documents': {b.norm_no('INV-0001'): {'treatment': 'sale_exempt', 'accepted': True, 'note': 'training'}}}
        r = self.sale(rules=rules)
        self.assertEqual((r['treatment'], r['needs_review']), ('sale_exempt', False))
        self.assertEqual(b.build_row('k1', doc(invoice_number='INV-0001'), rules, {}, Fx())['treatment'], 'domestic_input')  # a sale answer never moves a purchase

    def test_boxes_and_form(self):
        rows = [b.build_row('k0', doc(), RULES, {}, Fx()), self.sale(), self.sale(key='s2', invoice_number='INV-0002', net_amount=200.0, vat_amount=14.0, total_amount=214.0, vat_rate=7),
                self.sale(key='s3', invoice_number='INV-0003', net_amount=-100.0, vat_amount=-18.0, total_amount=-118.0),  # credit note
                self.no_vat(key='s4', invoice_number='INV-0004', customer_country='DE', customer_vat='DE123456789'),
                self.no_vat(key='s5', invoice_number='INV-0005', customer_country='US')]
        x = b.return_boxes(rows)
        self.assertEqual((x['1'], x['2'], x['18'], x['23'], x['18a'], x['23a'], x['22'], x['25'], x['26']), (1000.0, 1000.0, 900.0, 162.0, 200.0, 14.0, 1100.0, 176.0, 176.0))
        self.assertEqual((x['37'], x['42'], x['43']), (18.0, 0.0, 158.0))
        f = b.return_form(rows, brought=100.0)
        self.assertEqual((f['39'], f['26'], f['43'], f['44'], f['45']), (18.0, 176.0, 158.0, 100.0, 58.0))
        self.assertEqual(b.return_form(rows, brought=500.0)['45'], 0.0)
        self.assertTrue(all(ok for _, ok, _ in b.table_checks(rows, '2026-Q3')))
        twice = rows + [self.sale(key='s9')]
        self.assertFalse(dict((n, ok) for n, ok, _ in b.table_checks(twice, '2026-Q3'))['no sales invoice number is used twice'])
        self.assertEqual(b.recap_statement(rows), [['DE', 'DE123456789', 'Hotel Ltd', 'services', '1000.00']])
        self.assertEqual((b.row_tags(rows[1])[0][:2], b.row_tags(rows[4])[0][0]), (['Box 18/23', 'sale'], 'Box 1'))

    def test_purchases_alone_are_unchanged(self):  # the form of a quarter without sales: no output VAT, credit = input VAT
        f = b.return_form([b.build_row('k0', doc(), RULES, {}, Fx())])
        self.assertEqual((f['25'], f['26'], f['42'], f['43'], f['45']), (0.0, 0.0, 18.0, 0.0, 0.0))

    def test_missing_numbers_in_the_series(self):
        rows = [self.sale(key=f's{n}', invoice_number=f'INV-{n:04d}') for n in (7, 8, 10, 12)]
        self.assertEqual(b.sales_gaps(rows), ['INV-0009', 'INV-0011'])
        self.assertEqual(b.sales_gaps(rows + [self.sale(key='x', invoice_number='2026/A')]), [])  # not one numbered series: nothing is claimed

    def test_payments_received(self):
        rows = [self.sale(), b.build_row('k0', doc(), RULES, {}, Fx())]
        tx = lambda day, amount, text: {'date': day, 'amount': amount, 'currency': 'EUR', 'text': text, 'account': 'acc', 'used': False}
        txs = [tx('2026-09-20', 1180.0, 'TRANSFER FROM HOTEL LTD'), tx('2026-08-02', 118.0, 'refund'), tx('2026-08-03', -118.0, 'ACME'), tx('2026-08-09', 250.0, 'J Borg')]
        left = b.match_payments(rows, txs, {})
        self.assertTrue(rows[0]['payment'].startswith('2026-09-20 acc EUR 1180.00'))  # money in, 50 days later
        self.assertTrue(rows[1]['payment'].startswith('2026-08-03'))  # the purchase takes the money out, not the refund of the same amount
        self.assertEqual((left, [t['text'] for t in b.unmatched_income(txs, {})]), ([], ['J Borg', 'refund']))

    def test_sales_list_and_files_end_to_end(self):
        import tempfile
        out = Path(tempfile.mkdtemp())
        (out / 'sales').mkdir()
        (out / 'sales' / 'list.csv').write_text('Invoice no;Date;Client;Country;VAT number;Net;VAT;Total;VAT rate\n'
                                                'INV-0001;03/08/2026;Hotel Ltd;MT;;"1.000,00";180,00;"1.180,00";18%\n'
                                                'INV-0002;10/08/2026;Medien GmbH;DE;DE123456789;500,00;0;500,00;0\n'
                                                'INV-0009;02/10/2026;Next quarter;MT;;10,00;1,80;11,80;18\n')
        from PIL import Image
        Image.new('RGB', (200, 200), 'white').save(out / 'sales' / 'INV-0002.pdf', 'PDF')
        ledger = {'sale:INV-0002.pdf': {'vendor': '', 'date': '2026-08-10', 'invoice_number': 'INV 0002', 'source': 'sales', 'files': []}}  # read earlier
        rows = b.build_sales(ledger, out, out / '2026-Q3', b.quarter_range('2026-Q3'), None, RULES)
        self.assertEqual([(r['invoice_no'], r['treatment'], r['net_eur'], r['source_file']) for r in rows],
                         [('INV-0001', 'sale_domestic', 1000.0, ''), ('INV-0002', 'sale_eu_reverse_charge', 500.0, 'sales/INV-0002.pdf')])
        self.assertTrue((out / '2026-Q3' / 'sales' / 'INV-0002.pdf').exists())
        self.assertIn('customer VAT number: not checked', rows[1]['review_reason'])  # VIES is off in the tests
        self.assertEqual(b.read_sales_csv(out / 'sales' / 'INV-0002.pdf'), [])


class Demo(unittest.TestCase):
    def test_made_up_quarter_builds_and_every_check_passes(self):
        import tempfile
        import demo
        chrome, b.find_chrome = b.find_chrome, lambda: None  # no index pages: keeps the test fast
        try:
            qdir, label = demo.build(Path(tempfile.mkdtemp()) / 'demo', '2026-Q3')
        finally:
            b.find_chrome = chrome
        data = json.loads((qdir / '.review.json').read_text())
        rows = data['rows']
        self.assertEqual([c[0] for c in b.table_checks(rows, label) if not c[1]], [])
        got = {r['treatment'] for r in rows}
        self.assertTrue({'domestic_input', 'reverse_charge_eu', 'reverse_charge_non_eu', 'oss_risk', 'duplicate', 'sale_domestic', 'sale_eu_reverse_charge', 'sale_outside_malta'} <= got)
        f = b.return_form(rows)
        sales = [r for r in rows if r.get('direction') == 'sale']
        self.assertAlmostEqual(f['25'], sum(r['vat_eur'] for r in sales if r['treatment'] == 'sale_domestic'), places=2)
        self.assertAlmostEqual(f['43'] - f['42'], f['26'] - f['39'], places=2)
        self.assertEqual((len(data['unbilled']), len(data['unpaid']), b.sales_gaps(rows)), (1, 1, ['INV-0047']))
        for name in (f'{label}_VAT_worksheet.md', f'{label}_recapitulative_statement.csv', f'{label}_claimable.pdf', 'summary.csv'):
            self.assertTrue((qdir / name).exists(), name)


class Golden(unittest.TestCase):
    """output/golden.json: {"2026-Q3": {"boxes": {"3": 543.67, ...}, "documents": {"<invoice no>": [net_eur, vat_eur], ...}}}"""

    def test_filed_quarters(self):
        out = Path(os.environ.get('BOOKKEEPER_OUT', b.HERE / 'output'))
        if not (out / 'golden.json').exists():
            self.skipTest('no output/golden.json')
        for label, want in json.loads((out / 'golden.json').read_text()).items():
            path = out / label / 'summary.csv'
            if not path.exists():
                continue
            num = lambda v: float(v) if v not in ('', None) else None
            if 'treatment' not in next(csv.reader(open(path))):
                continue  # written before the table existed: run --final for that quarter first
            rows = [{**r, **{k: num(r[k]) for k in ('net', 'vat', 'gross', 'net_eur', 'vat_eur', 'gross_eur', 'rc_vat_eur')}} for r in csv.DictReader(open(path))]
            with self.subTest(label=label, part='boxes'):
                got = b.return_boxes(rows)
                self.assertEqual({k: got[k] for k in want['boxes']}, want['boxes'])
            claimed = {b.norm_no(r['invoice_no']): r for r in rows if r['treatment'] in b.CLAIM + b.RC}
            for no, (net, vat) in want.get('documents', {}).items():
                with self.subTest(label=label, invoice=no):
                    r = claimed.get(b.norm_no(no))
                    self.assertIsNotNone(r, 'not in the claimed set')
                    self.assertEqual((r['net_eur'], r['rc_vat_eur'] if r['treatment'] in b.RC else r['vat_eur']), (net, vat))
            with self.subTest(label=label, part='nothing else claimed'):
                self.assertEqual(sorted(set(claimed) - {b.norm_no(k) for k in want.get('documents', claimed)}), [])


if __name__ == '__main__':
    unittest.main()
