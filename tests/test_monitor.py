import unittest
import monitor


class MonitorTest(unittest.TestCase):
    def test_recruitment_accepts_2028_and_rejects_internship(self):
        src = {'id': 'test', 'name': 'Example', 'type': 'recruitment', 'url': 'https://example.com/'}
        self.assertTrue(monitor.is_relevant(src, '2028卒 本選考の応募受付開始、締切11月16日'))
        self.assertFalse(monitor.is_relevant(src, '2028卒 インターンシップ応募受付開始'))
        self.assertFalse(monitor.is_relevant(src, '2027卒 本選考の応募受付開始'))
    def test_html_extraction(self):
        src = {'id': 'test', 'name': 'Example', 'type': 'recruitment', 'url': 'https://example.com/'}
        html = '''<ul><li><a href="/grad28">2028卒 本選考募集開始（SA職）</a> 締切 11月16日</li>
        <li><a href="/intern">2028卒 インターンシップ募集開始</a></li></ul>'''
        items = monitor.extract_html(src, html)
        self.assertTrue(any('grad28' in x['url'] for x in items))
        self.assertFalse(any('intern' in x['url'] for x in items))
        self.assertTrue(any('11月16日' in x['detail'] for x in items))
    def test_same_title_date_change_same_id(self):
        src = {'id': 'test', 'name': 'Example', 'type': 'recruitment', 'url': 'https://example.com/'}
        a = monitor.make_item(src, '2028卒 本選考応募', '/apply', '締切 11月16日')
        b = monitor.make_item(src, '2028卒 本選考応募', '/apply', '締切 12月1日')
        self.assertEqual(a['id'], b['id'])
        self.assertNotEqual(a['fingerprint'], b['fingerprint'])
    def test_no_untrusted_url(self):
        src = {'id': 'test', 'name': 'Example', 'type': 'recruitment', 'url': 'https://example.com/'}
        self.assertEqual(monitor.safe_url('javascript:alert(1)', src['url']), src['url'])

if __name__ == '__main__':
    unittest.main()
