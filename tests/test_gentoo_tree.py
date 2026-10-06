"""Logical Win32 TreeView regressions using fake items only, without any UI."""
import re
import unittest
from types import SimpleNamespace

from app.gentoo_tree import NativeContact, parent_selectors, snapshot_tree
from app.windows import AutomationError


class FakeTree:
    def __init__(self, nodes, roots, counts=None):
        self.nodes, self.root_ids, self.counts = nodes, roots, counts
        self.count_reads, self.reads, self.actions = 0, [], []
        self.handle = 123456
        self.allow_actions = False
        self.next_ids = {}
        self.parent_window = SimpleNamespace(name='Fixture top-level window')
        for ids in [roots, *(node.get('children', []) for node in nodes.values())]:
            for index, handle in enumerate(ids):
                self.next_ids[handle] = ids[index + 1] if index + 1 < len(ids) else None

    def item_count(self):
        self.reads.append('item_count')
        if self.counts is None:
            return len(self.nodes)
        value = self.counts[min(self.count_reads, len(self.counts) - 1)]
        self.count_reads += 1
        return value

    def tree_root(self):
        self.reads.append('tree_root')
        return NativeItem(self.root_ids[0], self) if self.root_ids else None

    def send_message(self, message, flag, handle):
        self.reads.append(('send_message', message, flag, handle))
        if (message, flag) != (0x110A, 4):
            raise AssertionError('Only a read of the first logical child is allowed')
        node = self.nodes[handle]
        if node.get('missing_child'):
            return 0
        return node.get('children', [0])[0]

    def process_id(self):
        return 1234

    def top_level_parent(self):
        return self.parent_window

    def is_enabled(self):
        return True

    def action(self, action):
        self.actions.append(action)
        if not self.allow_actions:
            raise AssertionError('Snapshot must not change the native UI')

    def set_focus(self):
        self.action('tree_focus')


class NativeItem:
    def __init__(self, elem, tree_ctrl):
        self.elem, self.tree_ctrl = elem, tree_ctrl

    def text(self):
        self.tree_ctrl.reads.append(('text', self.elem))
        text = self.tree_ctrl.nodes[self.elem]['text']
        if isinstance(text, Exception):
            raise text
        return text

    def item(self):
        self.tree_ctrl.reads.append(('item', self.elem))
        node = self.tree_ctrl.nodes[self.elem]
        return SimpleNamespace(cChildren=node.get('child_flag', int(bool(node.get('children')))))

    def next_item(self):
        self.tree_ctrl.reads.append(('next_item', self.elem))
        next_id = self.tree_ctrl.next_ids[self.elem]
        return NativeItem(next_id, self.tree_ctrl) if next_id is not None else None

    def children(self):
        raise AssertionError('Native snapshot must avoid the library unbounded children loop')

    def expand(self):
        self.tree_ctrl.action('expand')

    def ensure_visible(self):
        self.tree_ctrl.action(('ensure_visible', self.elem))

    def click_input(self, **kwargs):
        self.tree_ctrl.action(('click_input', self.elem, kwargs))

    def click(self, **kwargs):
        self.tree_ctrl.action(('click', self.elem, kwargs))

    def select(self):
        self.tree_ctrl.action(('select', self.elem))


def forest():
    return FakeTree({
        1: {'text': 'Fixture School A', 'children': [2]},
        2: {'text': 'Fixture Department (1/3)', 'children': [3, 4], 'expanded': False},
        3: {'text': '(FixtureOnline(42))Department/42', 'visible': True},
        4: {'text': '(FixtureOffline(43)(43))Department/43', 'visible': False},
        5: {'text': 'Fixture School B', 'children': [6]},
        6: {'text': '(FixtureScrolledOut(44))Department/44', 'visible': False},
    }, [1, 5])


class LogicalTreeTests(unittest.TestCase):
    def test_collapsed_hidden_items_and_all_root_siblings_are_read_without_actions(self):
        tree = forest()
        records = snapshot_tree(tree)
        self.assertEqual([record['item'].elem for record in records], [1, 2, 3, 4, 5, 6])
        self.assertEqual(records[3]['text'], '(FixtureOffline(43)(43))Department/43')
        self.assertEqual(records[3]['parents'], ['Fixture Department (1/3)', 'Fixture School A'])
        self.assertEqual(records[-1]['parents'], ['Fixture School B'])
        self.assertEqual(records[0]['parents'], [])
        self.assertEqual(tree.actions, [])
        self.assertEqual(tree.reads.count('item_count'), 2)

    def test_empty_logical_tree_returns_empty_snapshot(self):
        tree = FakeTree({}, [])
        self.assertEqual(snapshot_tree(tree), [])
        self.assertEqual(tree.actions, [])

    def test_sibling_cycle_is_detected_by_handle_even_with_new_wrappers(self):
        tree = forest()
        tree.next_ids[4] = 3
        with self.assertRaisesRegex(AutomationError, '순환'):
            snapshot_tree(tree)
        self.assertEqual(tree.actions, [])

    def test_parent_cycle_and_repeated_handles_never_produce_a_partial_snapshot(self):
        tree = forest()
        tree.nodes[4]['children'] = [2]
        with self.assertRaisesRegex(AutomationError, '순환|중복'):
            snapshot_tree(tree)
        self.assertEqual(tree.actions, [])

    def test_reported_count_mismatch_and_change_are_distinct_errors(self):
        for counts, expected_error in (([7, 7], '읽은 항목 수'), ([6, 7], '읽는 동안 바뀌')):
            with self.subTest(counts=counts):
                tree = forest()
                tree.counts = counts
                with self.assertRaisesRegex(AutomationError, expected_error):
                    snapshot_tree(tree)
                self.assertEqual(tree.actions, [])

    def test_missing_advertised_children_and_unsupported_child_flags_are_rejected(self):
        for field, value, error in (('missing_child', True, '하위 항목이 있지만'),
                                    ('child_flag', -1, '존재 여부')):
            with self.subTest(field=field):
                tree = forest()
                tree.nodes[2][field] = value
                with self.assertRaisesRegex(AutomationError, error):
                    snapshot_tree(tree)
                self.assertEqual(tree.actions, [])

    def test_depth_sixteen_is_accepted_but_seventeen_is_rejected(self):
        for deepest in (16, 17):
            nodes = {index: {'text': f'Fixture node {index}', **({'children': [index + 1]} if index <= deepest else {})}
                     for index in range(1, deepest + 2)}
            tree = FakeTree(nodes, [1])
            with self.subTest(depth=deepest):
                if deepest == 16:
                    records = snapshot_tree(tree)
                    self.assertEqual(len(records[-1]['parents']), 16)
                else:
                    with self.assertRaisesRegex(AutomationError, '깊이'):
                        snapshot_tree(tree)
            self.assertEqual(tree.actions, [])

    def test_node_limit_is_enforced_on_counts_and_actual_traversal(self):
        tree = forest()
        with self.assertRaisesRegex(AutomationError, '한도'):
            snapshot_tree(tree, max_nodes=5)
        self.assertNotIn('tree_root', tree.reads)
        tree = forest()
        tree.counts = [5, 5]
        with self.assertRaisesRegex(AutomationError, '한도'):
            snapshot_tree(tree, max_nodes=5)
        self.assertEqual(tree.actions, [])

    def test_five_thousand_nodes_are_accepted_and_larger_count_is_rejected(self):
        tree = FakeTree({index: {'text': f'Fixture {index}'} for index in range(1, 5001)}, list(range(1, 5001)))
        self.assertEqual(len(snapshot_tree(tree)), 5000)
        tree = FakeTree({}, [], counts=[5001])
        with self.assertRaisesRegex(AutomationError, '한도'):
            snapshot_tree(tree)
        self.assertEqual(tree.actions, [])

    def test_text_failures_or_invalid_text_abort_without_actions(self):
        for text in (RuntimeError('Fixture read error'), '', 'Fixture\nUnexpected second line', 'Fixture\x00Invalid'):
            with self.subTest(text_type=type(text).__name__):
                tree = forest()
                tree.nodes[4]['text'] = text
                with self.assertRaises(AutomationError):
                    snapshot_tree(tree)
                self.assertEqual(tree.actions, [])

    def test_adapter_children_fallback_is_read_only_and_preserves_parent_path(self):
        class Item:
            def __init__(self, elem, text, children=()):
                self.elem, self.label, self.nodes = elem, text, list(children)
            def text(self): return self.label
            def children(self): return self.nodes
            def next_item(self): return None
            def expand(self): raise AssertionError('Do not expand a snapshot')
            def click_input(self): raise AssertionError('Do not click a snapshot')
        child = Item(12, 'Fixture child')
        root = Item(11, 'Fixture parent', [child])
        tree = SimpleNamespace(item_count=lambda: 2, tree_root=lambda: root)
        records = snapshot_tree(tree)
        self.assertEqual(records[1]['parents'], ['Fixture parent'])


class ParentAndAdapterTests(unittest.TestCase):
    def test_parent_selector_changes_only_trailing_group_count(self):
        selectors = parent_selectors(['Fixture Department (1/3)', 'Fixture School [A].'])
        self.assertEqual([selector['control_type'] for selector in selectors], ['TreeItem', 'TreeItem'])
        for text in ('Fixture Department', 'Fixture Department (0)', 'Fixture Department (4/7)'):
            self.assertTrue(re.fullmatch(selectors[0]['name_regex'], text))
        for text in ('Other Department (1/3)', 'Fixture DepartmentExtra (1/3)', 'Fixture Department (1/3) extra'):
            self.assertFalse(re.fullmatch(selectors[0]['name_regex'], text))
        self.assertTrue(re.fullmatch(selectors[1]['name_regex'], 'Fixture School [A].'))
        self.assertFalse(re.fullmatch(selectors[1]['name_regex'], 'Fixture School A!'))

    def test_native_contact_constructor_and_read_delegation_do_not_change_ui(self):
        tree = forest()
        contact = NativeContact(NativeItem(4, tree))
        self.assertEqual(contact.element_info.control_type, 'TreeItem')
        self.assertEqual(contact.element_info.name, tree.nodes[4]['text'])
        self.assertEqual(contact.window_text(), tree.nodes[4]['text'])
        self.assertEqual(contact.process_id(), 1234)
        self.assertIs(contact.top_level_parent(), tree.parent_window)
        self.assertTrue(contact.is_enabled())
        self.assertEqual(tree.actions, [])

    def test_other_explicit_adapter_actions_keep_their_original_delegation(self):
        tree = forest()
        contact = NativeContact(NativeItem(4, tree))
        self.assertEqual(tree.actions, [])
        tree.allow_actions = True  # Recording fake only, never a native window.
        contact.select()
        contact.set_focus()
        self.assertEqual(tree.actions, [('select', 4), 'tree_focus'])


if __name__ == '__main__':
    unittest.main()
