"""Observed CEF paragraph decoding and exact body verification; no live UI."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import gentoo, windows


class GentooParagraphTests(unittest.TestCase):
    def test_hard_breaks_and_literal_symbols_are_reconstructed(self):
        parts = ['쿨 예약실 입력 시험입니다. ', '두 번째 줄입니다. ',
                 '특수문자: { } + ^ % ~ ( ) ', '이모지: 🚀 ']
        expected = '쿨 예약실 입력 시험입니다.\n두 번째 줄입니다.\n특수문자: { } + ^ % ~ ( )\n이모지: 🚀'
        self.assertEqual(gentoo.decode_gentoo_paragraphs(parts), expected)

    def test_two_empty_paragraphs_preserve_three_line_breaks(self):
        self.assertEqual(gentoo.decode_gentoo_paragraphs(['첫째 줄 ', '\n ', '\n ', '넷째 줄 ']),
                         '첫째 줄\n\n\n넷째 줄')

    def test_global_outer_whitespace_follows_existing_saved_job_normalization(self):
        self.assertEqual(gentoo.decode_gentoo_paragraphs(['\n ', '  본문   ', '\n ']), '본문')

    def test_soft_wrapped_long_text_remains_one_paragraph(self):
        expected = '줄바꿈 없이 길게 입력한 한글 문장과 {ENTER} +^%~() 🚀. ' * 30 + '끝'
        self.assertEqual(gentoo.decode_gentoo_paragraphs([expected + ' ']), expected)

    def test_exactly_one_synthetic_space_is_removed(self):
        parts = ['시작 ', '  내부 공백 끝    ', '\n ', '\n ', '  다음 줄 끝    ', '마지막 ']
        self.assertEqual(gentoo.decode_gentoo_paragraphs(parts),
                         '시작\n  내부 공백 끝   \n\n\n  다음 줄 끝   \n마지막')

    def test_observed_nbsp_alternation_is_kept_by_decoder(self):
        first = '\u00a0 시작\u00a0 두칸 + ^ % ~ 끝\u00a0 \u00a0 '
        last = '\u00a0 다음 줄\u00a0 연속 공백 끝\u00a0 \u00a0 '
        actual = gentoo.decode_gentoo_paragraphs(['시작 ', first, '\n ', '\n ', last, '마지막 '])
        self.assertEqual(actual,
                         '시작\n\u00a0 시작\u00a0 두칸 + ^ % ~ 끝\u00a0 \u00a0\n\n\n'
                         '\u00a0 다음 줄\u00a0 연속 공백 끝\u00a0 \u00a0\n마지막')
        self.assertTrue(windows.body_matches(actual,
                        '시작\n  시작  두칸 + ^ % ~ 끝   \n\n\n  다음 줄  연속 공백 끝   \n마지막'))

    def test_missing_markers_and_interior_line_breaks_are_rejected(self):
        malformed = ('본문', '본문\u00a0', '첫 줄\n둘째 줄 ', '\n본문 ', '\n\n ', '본문\r다음 줄 ')
        for raw in malformed:
            with self.subTest(raw=repr(raw)), self.assertRaises(windows.AutomationError):
                gentoo.decode_gentoo_paragraphs([raw])


class GentooBodyMatchTests(unittest.TestCase):
    def test_exact_text_and_allowed_nbsp_positions_match(self):
        expected = '  한글  끝   \n\n🚀 {ENTER} +^%~()'
        self.assertTrue(windows.body_matches(expected, expected))
        self.assertTrue(windows.body_matches('\u00a0 한글\u00a0 끝\u00a0 \u00a0\n\n🚀 {ENTER} +^%~()', expected))
        self.assertTrue(windows.body_matches('원문\u00a0NBSP', '원문\u00a0NBSP'))

    def test_user_nbsp_cannot_be_replaced_by_an_ordinary_space(self):
        self.assertFalse(windows.body_matches('원문 NBSP', '원문\u00a0NBSP'))

    def test_line_break_and_empty_line_mismatches_do_not_match(self):
        mismatches = (
            ('첫 줄 둘째 줄', '첫 줄\n둘째 줄'),
            ('첫 줄\n둘째 줄', '첫 줄\n\n둘째 줄'),
            ('첫 줄\n\n둘째 줄', '첫 줄\n둘째 줄'),
            ('시작\n본문\n끝', '시작\n\n본문\n끝'),
        )
        for actual, expected in mismatches:
            with self.subTest(actual=repr(actual), expected=repr(expected)):
                self.assertFalse(windows.body_matches(actual, expected))

    def test_space_counts_and_other_unicode_whitespace_do_not_match(self):
        mismatches = (
            ('처음\n 시작\n마지막', '처음\n  시작\n마지막'),
            ('처음\n끝 \n마지막', '처음\n끝  \n마지막'), ('두 칸', '두  칸'),
            ('두\u00a0칸', '두  칸'), ('두\t칸', '두 칸'), ('두\u2002칸', '두 칸'),
        )
        for actual, expected in mismatches:
            with self.subTest(actual=repr(actual), expected=repr(expected)):
                self.assertFalse(windows.body_matches(actual, expected))

    def test_emoji_symbol_and_character_loss_do_not_match(self):
        for actual in ('이모지: ', '이모지: �', '이모지: 🛰', '이모지: \ud83d'):
            with self.subTest(actual=ascii(actual)):
                self.assertFalse(windows.body_matches(actual, '이모지: 🚀'))
        self.assertFalse(windows.body_matches('{ } + ^ % ( )', '{ } + ^ % ~ ( )'))
        self.assertFalse(windows.body_matches('첫 부분', '첫 부분과 뒷 부분'))

    def test_existing_global_trim_and_crlf_policy_is_unchanged(self):
        self.assertTrue(windows.body_matches(' \n첫 줄\r\n둘째 줄\n ', '첫 줄\n둘째 줄'))


class MemoryTextRange:
    """UIA range endpoint contract over a fixed in-memory text provider."""
    def __init__(self, provider, start, end, kind='cursor'):
        self.provider = provider
        self.start = start
        self.end = end
        self.kind = kind

    def Clone(self):
        return MemoryTextRange(self.provider, self.start, self.end)

    def GetText(self, limit):
        text = self.provider.raw[self.start:self.end]
        if self.kind == 'group':
            self.provider.group_reads += 1
            if self.provider.change_group and self.provider.group_reads > 1:
                return text + '변경'
        if self.kind == 'cursor' and self.provider.incomplete_cursor and self.start > 0:
            return text[:-1]
        return text

    def CompareEndpoints(self, endpoint, target, target_endpoint):
        own = self.start if endpoint == 0 else self.end
        other = target.start if target_endpoint == 0 else target.end
        return own - other

    def MoveEndpointByRange(self, endpoint, target, target_endpoint):
        value = target.start if target_endpoint == 0 else target.end
        if endpoint == 0:
            self.start = value
            self.end = max(self.end, self.start)
        else:
            self.end = value
            self.start = min(self.start, self.end)

    def MoveEndpointByUnit(self, endpoint, unit, count):
        if (endpoint, unit, count) != (1, 4, 1):
            raise AssertionError('Only one paragraph-end movement is supported by this fixture')
        if self.provider.no_movement:
            return 0
        if self.provider.no_progress:
            return 1
        boundaries = [value for value in self.provider.boundaries if value > self.end]
        if not boundaries:
            return 0
        self.end = boundaries[0]
        return 1


class MemoryParagraphProvider:
    def __init__(self, *, no_movement=False, no_progress=False,
                 incomplete_cursor=False, change_group=False):
        parts = ['처음 ', '\n ', '\n ', '이모지 🚀 끝 ']
        self.group_raw = ''.join(parts)
        self.raw = self.group_raw + '  '  # Document-only accessibility sentinels.
        offset = 0
        self.boundaries = []
        for part in parts:
            offset += len(part)
            self.boundaries.append(offset)
        # Native paragraph movement can overshoot the child group's endpoint.
        self.boundaries[-1] = len(self.raw)
        self.no_movement = no_movement
        self.no_progress = no_progress
        self.incomplete_cursor = incomplete_cursor
        self.change_group = change_group
        self.group_reads = 0
        self.group_element = object()
        self.DocumentRange = MemoryTextRange(self, 0, len(self.raw), 'document')
        self.RangeFromChild = Mock(side_effect=self.group_range)

    def group_range(self, element):
        if element is not self.group_element:
            raise AssertionError('Wrong editable group')
        return MemoryTextRange(self, 0, len(self.group_raw), 'group')

    def document(self, groups=1):
        return SimpleNamespace(
            iface_text=self,
            descendants=lambda **kwargs: [SimpleNamespace(element_info=SimpleNamespace(element=self.group_element))]
                                         * groups,
        )


class GentooBodyRangeTests(unittest.TestCase):
    def test_complete_group_excludes_document_sentinels_and_clamps_last_paragraph(self):
        provider = MemoryParagraphProvider()
        self.assertEqual(gentoo.read_gentoo_body(provider.document()), '처음\n\n\n이모지 🚀 끝')
        provider.RangeFromChild.assert_called_once_with(provider.group_element)

    def test_ambiguous_or_missing_editable_group_is_rejected(self):
        for groups in (0, 2):
            with self.subTest(groups=groups), self.assertRaisesRegex(windows.AutomationError, '유일하게'):
                gentoo.read_gentoo_body(MemoryParagraphProvider().document(groups))

    def test_failed_movement_or_nonprogress_cannot_loop_or_return_partial_text(self):
        for config in ({'no_movement': True}, {'no_progress': True}):
            with self.subTest(config=config), self.assertRaises(windows.AutomationError):
                gentoo.read_gentoo_body(MemoryParagraphProvider(**config).document())

    def test_missing_coverage_or_change_while_reading_is_rejected(self):
        for config in ({'incomplete_cursor': True}, {'change_group': True}):
            with self.subTest(config=config), self.assertRaisesRegex(windows.AutomationError, '빠짐없이'):
                gentoo.read_gentoo_body(MemoryParagraphProvider(**config).document())


class GentooBodyAdapterTests(unittest.TestCase):
    def driver(self, roles):
        driver = windows.WindowsDriver({'roles': roles}, threading.Event())
        driver.focus = Mock()
        driver.wait = Mock()
        return driver

    def test_put_reads_paragraph_body_for_existing_and_explicit_bindings(self):
        expected = '처음\n\n두 번째 줄 🚀'
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                parent_id = 'other-frame' if explicit else 'editFrame'
                document = SimpleNamespace(
                    element_info=SimpleNamespace(control_type='Document'),
                    parent=lambda: SimpleNamespace(element_info=SimpleNamespace(automation_id=parent_id)),
                    type_keys=Mock(),
                    iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _: '처음 두 번째 줄 🚀')),
                )
                driver = self.driver({'body': {'reader': 'gentoo_body'} if explicit else {}})
                driver.role = Mock(return_value=document)
                with patch.object(gentoo, 'read_gentoo_body', return_value=expected) as read_body, \
                        patch.object(windows, 'read_text', side_effect=AssertionError('Flattened readback used')):
                    driver.put('body', expected)
                read_body.assert_called_once_with(document)
                self.assertEqual(document.type_keys.call_count, 2)

    def test_other_document_uses_its_original_text_reader(self):
        document = SimpleNamespace(
            element_info=SimpleNamespace(control_type='Document'),
            parent=lambda: SimpleNamespace(element_info=SimpleNamespace(automation_id='other-frame')),
            iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _: '다른 편집기 본문')),
        )
        driver = self.driver({'body': {}})
        driver.role = Mock(return_value=document)
        with patch.object(gentoo, 'read_gentoo_body', side_effect=AssertionError('Wrong editor upgraded')):
            self.assertEqual(driver.text('body'), '다른 편집기 본문')


if __name__ == '__main__':
    unittest.main()
