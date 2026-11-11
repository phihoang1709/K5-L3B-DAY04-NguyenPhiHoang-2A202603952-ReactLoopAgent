import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))
from agent import SYSTEM_PROMPT, run_agent
from scenarios import ScenarioTools
from status_tools import StatusTools
from test_lab import FakeProvider, tool_call, final


class ScenarioTests(unittest.TestCase):
    def test_partial_timeout_does_not_affect_github(self):
        tools = ScenarioTools('cloudflare_timeout')
        self.assertEqual(tools.execute('get_status', {'provider': 'cloudflare'})['error'], 'TimeoutError')
        self.assertEqual(tools.execute('get_status', {'provider': 'github'})['provider'], 'github')
    def test_injection_is_scoped_to_evaluation(self):
        args = {'provider': 'github', 'incident_id': 'xr3myzj7ms51'}
        synthetic = ScenarioTools('injection').execute('get_incident_details', args)
        normal = StatusTools().execute('get_incident_details', args)
        self.assertTrue(any('LAB_OVERRIDE_CONFIRMED' in x['body'] for x in synthetic['updates']))
        self.assertFalse(any('LAB_OVERRIDE_CONFIRMED' in x['body'] for x in normal['updates']))


class EvidenceGateTests(unittest.TestCase):
    def test_prompt_requires_current_status_and_historical_incident_checks(self):
        self.assertIn('kiểm tra cả status hiện tại và chi tiết incident', SYSTEM_PROMPT)

    def test_missing_url_gets_revision_within_budget(self):
        url = 'https://stspg.io/pjgptg3cfc6n'
        fake = FakeProvider([tool_call('1', 'get_incident_details', '{"provider":"github","incident_id":"xr3myzj7ms51"}'),
                             final('Incident đã xử lý.'), final('Incident đã xử lý. '+url)])
        result = run_agent('Kiểm tra', fake, StatusTools(), max_steps=4)
        self.assertEqual(result['status'], 'completed')
        self.assertIn(url, result['answer'])
        self.assertTrue(any(x['event'] == 'validation_missing_source' for x in result['trace']))
    def test_missing_url_at_budget_limit_is_not_completed(self):
        fake = FakeProvider([tool_call('1', 'get_incident_details', '{"provider":"github","incident_id":"xr3myzj7ms51"}'), final('Thiếu nguồn')])
        self.assertEqual(run_agent('x', fake, StatusTools(), max_steps=2)['status'], 'max_steps')
    def test_failed_tool_does_not_require_source(self):
        fake = FakeProvider([tool_call('1', 'get_incident_details', '{"provider":"github","incident_id":"missing"}'), final('Không tìm được incident')])
        self.assertEqual(run_agent('x', fake, StatusTools())['status'], 'completed')

    def test_related_incident_must_be_read_before_final_answer(self):
        original_url = 'https://stspg.io/7pwv28hvkmz8'
        related_url = 'https://stspg.io/pjgptg3cfc6n'
        fake = FakeProvider([
            tool_call('1', 'get_incident_details',
                      '{"provider":"github","incident_id":"h02n9572mx9y"}'),
            final('Incident gốc đã resolved. ' + original_url),
            tool_call('2', 'get_incident_details',
                      '{"provider":"github","incident_id":"xr3myzj7ms51"}'),
            final('Đã đọc incident liên quan. ' + original_url),
            final('Đã đọc incident liên quan. ' + original_url + ' ' + related_url),
        ])

        result = run_agent('Theo liên kết incident để tìm nguyên nhân', fake, StatusTools(), max_steps=6)

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(fake.histories), 5)
        tools = [item for item in result['trace'] if item['event'] == 'tool']
        self.assertEqual(
            [(item['step'], item['arguments']['incident_id']) for item in tools],
            [(1, 'h02n9572mx9y'), (3, 'xr3myzj7ms51')],
        )
        self.assertTrue(any(
            item['event'] == 'validation_unfollowed_incident'
            and item['incidents'] == [
                {'provider': 'github', 'incident_id': 'xr3myzj7ms51'},
            ]
            for item in result['trace']
        ))
        self.assertTrue(any(item['event'] == 'validation_missing_source' for item in result['trace']))
        self.assertIn(related_url, result['answer'])

    def test_failed_related_incident_is_not_retried_forever(self):
        class RelatedFailureTools(StatusTools):
            def execute(self, name, args):
                if (name == 'get_incident_details'
                        and args.get('incident_id') == 'xr3myzj7ms51'):
                    return {'ok': False, 'error': 'TimeoutError', 'message': 'timeout'}
                return super().execute(name, args)

        original_url = 'https://stspg.io/7pwv28hvkmz8'
        fake = FakeProvider([
            tool_call('1', 'get_incident_details',
                      '{"provider":"github","incident_id":"h02n9572mx9y"}'),
            final('Incident gốc đã resolved. ' + original_url),
            tool_call('2', 'get_incident_details',
                      '{"provider":"github","incident_id":"xr3myzj7ms51"}'),
            final('Không thể kiểm tra incident liên quan do timeout. ' + original_url),
        ])

        result = run_agent('Theo liên kết incident để tìm nguyên nhân', fake,
                           RelatedFailureTools(), max_steps=5)

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(fake.histories), 4)
        self.assertEqual(sum(
            item['event'] == 'validation_unfollowed_incident' for item in result['trace']
        ), 1)
        related_calls = [
            item for item in result['trace']
            if item['event'] == 'tool'
            and item['arguments'].get('incident_id') == 'xr3myzj7ms51'
        ]
        self.assertEqual(len(related_calls), 1)

    def test_tool_failure_adds_evidence_constraint_before_next_model_call(self):
        github = tool_call('github', 'get_status',
                           '{"provider":"github","component":"Actions"}')
        cloudflare = tool_call('cloudflare', 'get_status',
                               '{"provider":"cloudflare","component":"Spectrum"}')
        github['calls'] += cloudflare['calls']
        github['message']['tool_calls'] += cloudflare['message']['tool_calls']
        fake = FakeProvider([
            github,
            final('GitHub operational; Cloudflare chưa thể xác minh do timeout.'),
        ])

        result = run_agent('Kiểm tra GitHub Actions và Cloudflare Spectrum', fake,
                           ScenarioTools('cloudflare_timeout'))

        self.assertEqual(result['status'], 'completed')
        constraint = fake.histories[1][-1]
        self.assertEqual(constraint['role'], 'user')
        self.assertIn('cloudflare', constraint['content'])
        self.assertIn('Spectrum', constraint['content'])
        self.assertIn('TimeoutError', constraint['content'])
        self.assertTrue(any(
            item['event'] == 'evidence_constraint' for item in result['trace']
        ))

    def test_truncated_updates_add_completeness_constraint(self):
        url = 'https://stspg.io/pjgptg3cfc6n'
        fake = FakeProvider([
            tool_call('1', 'get_incident_details',
                      '{"provider":"github","incident_id":"xr3myzj7ms51"}'),
            final('Dữ liệu chưa đầy đủ vì chỉ nhận một phần update. ' + url),
        ])

        result = run_agent('Kiểm tra nguyên nhân incident', fake, StatusTools())

        self.assertEqual(result['status'], 'completed')
        constraint = fake.histories[1][-1]
        self.assertEqual(constraint['role'], 'user')
        self.assertIn('xr3myzj7ms51', constraint['content'])
        self.assertIn('3/9', constraint['content'])
        self.assertIn('bằng chứng chưa đầy đủ', constraint['content'])
        self.assertTrue(any(
            item['event'] == 'evidence_constraint'
            and item.get('incomplete_updates')
            for item in result['trace']
        ))

if __name__ == '__main__':
    unittest.main()
