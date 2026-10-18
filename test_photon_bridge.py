import unittest
from unittest import mock

import photon_bridge


def msg(i, text='hi', kind='direct', sender_kind='human', status='received', created='2026-10-18T06:30:00.000Z'):
    return {'id': f'm{i}', 'platform': 'imessage', 'status': status, 'createdAt': created,
            'sentBy': {'id': 'u1', 'kind': sender_kind, 'handle': '+13105550101'},
            'receivedBy': {'id': 'agent1', 'kind': 'agent', 'handle': '+16285024649'},
            'space': {'id': 's1', 'kind': kind}, 'content': {'type': 'text', 'format': 'plain', 'text': text}}


class BridgeTests(unittest.TestCase):
    def run_once(self, pages):
        calls, polls = [], iter([False, True])

        def fake(method, path, body=None, headers=None, key=None):
            calls.append((method, path, body, headers))
            return pages.pop(0) if method == 'GET' else {}
        with mock.patch.object(photon_bridge, 'request', fake):
            photon_bridge.run('pho_prj_x', 'k', sleep=lambda s: None, stop=lambda: next(polls))
        return calls

    def test_replies_once_in_same_space_as_the_agent(self):
        calls = self.run_once([{'items': [msg(1, 'plan dinner'), msg(1, 'plan dinner')]}])
        sends = [c for c in calls if c[0] == 'POST']
        self.assertEqual(len(sends), 1)  # duplicate id ignored
        _, path, body, headers = sends[0]
        self.assertEqual(body['spaceId'], 's1')
        self.assertEqual(body['senderId'], 'agent1')
        self.assertNotIn('parentMessageId', body)  # threaded replies get 409 SPACE_NOT_SENDABLE on shared lines
        self.assertEqual(body['content'], {'type': 'text', 'format': 'plain', 'text': 'ConsensusAI is connected. You said: plan dinner'})
        self.assertEqual(headers['idempotency-key'], 'consensusai-reply:m1')

    def test_transient_send_errors_retry_and_permanent_ones_do_not(self):
        import io, urllib.error
        sends, polls = [], iter([False, False, True])

        def fake(method, path, body=None, headers=None, key=None):
            if method == 'GET':
                return {'items': [msg(1), msg(2)]}
            sends.append(headers['idempotency-key'])
            code = {1: 503, 2: 409}[len(sends)] if len(sends) <= 2 else None
            if code:
                raise urllib.error.HTTPError(path, code, 'err', {}, io.BytesIO(b'{"code":"SPACE_NOT_SENDABLE"}'))
            return {}
        with mock.patch.object(photon_bridge, 'request', fake):
            photon_bridge.run('pho_prj_x', 'k', sleep=lambda s: None, stop=lambda: next(polls))
        # poll 1: m1 hits 503 (retry later); poll 2: m1 hits 409 (give up), m2 succeeds
        self.assertEqual(sends, ['consensusai-reply:m1', 'consensusai-reply:m1', 'consensusai-reply:m2'])

    def test_ignores_groups_agents_and_outbound(self):
        calls = self.run_once([{'items': [msg(1, kind='group'), msg(2, sender_kind='agent'), msg(3, status='sent')]}])
        self.assertFalse([c for c in calls if c[0] == 'POST'])

    def test_follows_pages_and_mask_hides_numbers(self):
        calls = self.run_once([{'items': [msg(1)], 'nextPageToken': 't2'}, {'items': [msg(2)]}])
        self.assertIn('pageToken=t2', calls[2][1] if calls[1][0] == 'POST' else calls[1][1])
        self.assertEqual(len([c for c in calls if c[0] == 'POST']), 2)
        self.assertEqual(photon_bridge.mask('+13105550101'), '+1•••01')


if __name__ == '__main__':
    unittest.main()
