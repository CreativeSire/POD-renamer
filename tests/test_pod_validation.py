from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from routes.pod import extract_document, prepare_document_for_ai, select_history_candidates, should_use_history, validate_extraction
from routes.history import pagination_window


class ExtractionValidationTests(unittest.TestCase):
    def test_pagination_window_keeps_current_page_and_boundaries(self):
        self.assertEqual(pagination_window(83, 166), [1, 2, None, 81, 82, 83, 84, 85, None, 165, 166])

    def test_uses_history_only_after_the_document_only_check_is_ambiguous(self):
        self.assertFalse(should_use_history({'valid': True}))
        self.assertTrue(should_use_history({'valid': False}))
    def test_accepts_a_complete_dala_invoice_with_a_real_date(self):
        result = validate_extraction({
            'supermarket': 'Supersaver',
            'location': 'Osapa',
            'invoice': 'DT-035263',
            'date': '26-02-2026',
        }, 'dala')

        self.assertEqual(result['invoice'], '035263')
        self.assertEqual(result['date'], '26-02-2026')

    def test_rejects_an_impossible_dala_date(self):
        with self.assertRaisesRegex(ValueError, 'valid calendar date'):
            validate_extraction({
                'supermarket': 'Supersaver',
                'location': 'Osapa',
                'invoice': '035263',
                'date': '31-02-2026',
            }, 'dala')

    def test_keeps_an_unusual_invoice_for_visual_consensus_to_decide(self):
        result = validate_extraction({
            'supermarket': 'Emmatos Supermarket',
            'location': 'Aboru',
            'invoice': '08163956503',
            'date': '22-06-2026',
        }, 'dala')

        self.assertEqual(result['invoice'], '08163956503')

    def test_ranks_a_known_store_and_location_from_history(self):
        candidates = select_history_candidates(
            'Emmato Supermarket',
            'Aboru',
            [
                {'store_name': 'Jendol Supermarket', 'location': 'Alakuko', 'uses': 30},
                {'store_name': 'Emmatos Supermarket', 'location': 'Aboru', 'uses': 4},
            ],
        )

        self.assertEqual(candidates[0]['store_name'], 'Emmatos Supermarket')
        self.assertEqual(candidates[0]['location'], 'Aboru')

    def test_keeps_a_specific_multiword_location_when_the_pod_has_one(self):
        result = validate_extraction({
            'supermarket': 'Supersaver',
            'location': 'Chevron Drive Lekki Lagos',
            'invoice': '035263',
            'date': '26-02-2026',
        }, 'dala')

        self.assertEqual(result['location'], 'Chevron Drive Lekki Lagos')

    def test_rejects_a_brand_date_that_is_not_six_digits(self):
        with self.assertRaisesRegex(ValueError, 'DDMMYY'):
            validate_extraction({
                'brand': 'FlozzyD',
                'supermarket': 'Jendol',
                'location': 'Alakuko',
                'invoice': '06081',
                'date': '25-02-2026',
            }, 'brand')

    def test_prepares_a_large_png_as_a_bounded_jpeg_for_ai(self):
        source = BytesIO()
        Image.new('RGBA', (3200, 1600), 'white').save(source, format='PNG')

        prepared, mime_type = prepare_document_for_ai(source.getvalue(), 'image/png')

        self.assertEqual(mime_type, 'image/jpeg')
        with Image.open(BytesIO(prepared)) as result:
            self.assertLessEqual(max(result.size), 2600)
            self.assertEqual(result.mode, 'RGB')

    def test_keeps_a_pdf_unchanged_for_ai(self):
        source = b'%PDF-1.4 minimal test'

        prepared, mime_type = prepare_document_for_ai(source, 'application/pdf')

        self.assertEqual(prepared, source)
        self.assertEqual(mime_type, 'application/pdf')

    @patch('routes.pod.call_gemini')
    def test_requires_the_second_ai_quality_check_before_passing(self, call_gemini):
        call_gemini.side_effect = [
            {'supermarket': 'Supersaver', 'location': 'Osapa', 'invoice': '035263', 'date': '26-02-2026'},
            {'valid': False, 'reason': 'Invoice digits are unclear'},
        ]

        with self.assertRaisesRegex(ValueError, 'Quality check failed'):
            extract_document(b'image', 'image/jpeg', 'dala')

        self.assertEqual(call_gemini.call_count, 2)
