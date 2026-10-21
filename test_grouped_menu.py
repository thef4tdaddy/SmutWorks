import unittest

import adult_content_organizer as o
import grouped_menu as m


class MenuTests(unittest.TestCase):
    def test_six_sections_contain_every_action_once(self):
        self.assertEqual(len(m.SECTIONS), 6)
        ids = [key for _, values in m.SECTIONS for key in values]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(dict(o.MENU_ITEMS)) - {"0"})

    def test_enter_then_back_returns_to_selected_section(self):
        nav = m.Navigation()
        nav.index = 2
        self.assertIsNone(nav.key("ENTER"))
        self.assertEqual(nav.section, 2)
        nav.key("DOWN")
        nav.key("BACK")
        self.assertIsNone(nav.section)
        self.assertEqual(nav.index, 2)
        nav.key("ENTER")
        self.assertEqual(nav.index, 1)
        self.assertEqual(nav.key("ENTER"), "25")

    def test_numbers_are_local_and_zero_returns(self):
        nav = m.Navigation()
        nav.key("4")
        nav.key("ENTER")
        self.assertEqual(nav.section, 3)
        nav.key("3")
        self.assertEqual(nav.key("ENTER"), "11")
        nav.key("0")
        nav.key("ENTER")
        self.assertIsNone(nav.section)
        nav.key("0")
        self.assertEqual(nav.key("ENTER"), "0")

    def test_quit_in_submenu_goes_back_instead_of_exit(self):
        nav = m.Navigation(3)
        self.assertIsNone(nav.key("QUIT"))
        self.assertEqual(nav.key("QUIT"), "0")

    def test_home_end_wrap_and_invalid_selection(self):
        nav = m.Navigation()
        nav.key("UP")
        self.assertEqual(nav.index, 6)
        nav.key("HOME")
        self.assertEqual(nav.index, 0)
        nav.key("9")
        self.assertIsNone(nav.key("ENTER"))
        self.assertIsNone(nav.section)
        nav.key("END")
        self.assertEqual(nav.key("ENTER"), "0")

    def test_sidebar_contains_only_action_section(self):
        sidebar = m.sidebar("11", o.MENU_ITEMS)
        self.assertLess(len(sidebar), 8)
        self.assertIn("11", dict(sidebar))
        self.assertNotIn("24", dict(sidebar))
