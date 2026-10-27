import asyncio
import tempfile
import types
import unittest

from billing import MICRO, Billing, BillingDenied, BillingMixin, billed, cost, parse_prices, parse_yuan
from token_usage import generate_with_token_usage


class _Event:
    def __init__(self, group='100', uid='42'):
        self.group, self.uid, self.sent = group, uid, []

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.uid

    def plain_result(self, text):
        return text

    async def send(self, message):
        self.sent.append(message)


class _Provider:
    def __init__(self, model):
        self.model = model

    def get_model(self):
        return self.model


class _Plugin(BillingMixin):
    def __init__(self, data_dir, usage):
        self.DATA_DIR = data_dir
        self.config = {'billing_group_ids': ['100'], 'billing_free_qq': ['7'],
                       'billing_prices': '# 注释\ngpt 2 8\n* 1 1', 'billing_low_balance_yuan': 1.0}
        self.calls = 0

        async def llm_generate(**kwargs):
            self.calls += 1
            return types.SimpleNamespace(raw_completion={'usage': usage}, completion_text='答案' * 10)
        self.context = types.SimpleNamespace(llm_generate=llm_generate, get_provider_by_id=lambda pid: _Provider(pid))

    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    @billed
    async def solve(self, problem_text, event):
        await generate_with_token_usage(self, chat_provider_id='gpt', prompt=problem_text, image_urls=['a.png'])
        await generate_with_token_usage(self, chat_provider_id='other', prompt=problem_text)
        return 'pdf'


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def plugin(self, usage=None):
        return _Plugin(self.tmp.name, usage if usage is not None else {'prompt_tokens': 1000, 'completion_tokens': 500})

    def test_parsing_and_cost_are_exact(self):
        self.assertEqual(parse_yuan('10'), 10 * MICRO)
        self.assertEqual(parse_yuan('-0.000001'), -1)
        for bad in ('0.0000001', 'nan', 'inf', 'abc', 'sNaN'):
            self.assertIsNone(parse_yuan(bad), bad)
        with self.assertRaises(ValueError):
            parse_prices('gpt 1 2')  # no '*' row would make unknown models free
        prices = parse_prices('gpt 0.3 0.7\n* 1 1')
        self.assertEqual(cost(prices, 'gpt', 1000, 1), 301)  # 300 + 0.7 rounds up, no float drift
        self.assertEqual(cost(prices, 'unknown', 5, 5), 10)

    def test_codes_are_single_use(self):
        store = Billing(f'{self.tmp.name}/b.sqlite3')
        code, = store.create_codes(5 * MICRO, 1)
        self.assertEqual(store.redeem('42', code.lower()), (5 * MICRO, 5 * MICRO))
        self.assertIsNone(store.redeem('43', code))
        self.assertIsNone(store.redeem('43', 'NOPE'))
        self.assertEqual(store.balance('43'), 0)

    def test_billed_job_charges_each_call_and_blocks_at_zero(self):
        plugin, event = self.plugin(), _Event()
        self.assertIn('余额不足', plugin._billing_denial(event))
        with self.assertRaises(BillingDenied):
            asyncio.run(plugin.solve('题', event))
        self.assertEqual(plugin.calls, 0)

        plugin._billing().add('42', 2 * MICRO, 'admin')
        self.assertEqual(asyncio.run(plugin.solve('题', event)), 'pdf')
        # gpt: 1000*2 + 500*8 = 6000; other uses '*': 1000 + 500 = 1500
        self.assertEqual(plugin._billing().balance('42'), 2 * MICRO - 7500)
        self.assertEqual(plugin._billing().spent('42'), 7500)
        self.assertEqual(event.sent, [])  # ¥1.9925 is above the ¥1 reminder threshold

        plugin._billing().add('42', -2 * MICRO + 7500 + 100, 'admin')
        asyncio.run(plugin.solve('题', event))  # allowed at ¥0.0001, may end negative
        self.assertEqual(plugin._billing().balance('42'), 100 - 7500)
        self.assertEqual(event.sent, ['本次消耗 ¥0.0075，余额 ¥-0.0074，请及时兑换充值'])
        self.assertIn('余额不足', plugin._billing_denial(event))

    def test_free_contexts_are_not_charged(self):
        plugin = self.plugin()
        for event in (_Event(group='200'), _Event(group=''), _Event(uid='7')):
            self.assertEqual(plugin._billing_denial(event), '')
            asyncio.run(plugin.solve('题', event))
        plugin.config['billing_enabled'] = False
        asyncio.run(plugin.solve('题', _Event()))
        self.assertEqual(plugin.calls, 8)
        self.assertEqual(plugin._billing().spent('42') + plugin._billing().spent('7'), 0)

    def test_missing_usage_is_estimated(self):
        plugin, event = self.plugin(usage={}), _Event()
        plugin._billing().add('42', MICRO, 'admin')
        asyncio.run(plugin.solve('题目', event))
        # input 2 chars (+1000 for the image on the gpt call), output 20 chars
        self.assertEqual(plugin._billing().spent('42'), (1002 * 2 + 20 * 8) + (2 + 20))

    def test_misconfigured_prices_fail_closed(self):
        plugin = self.plugin()
        plugin._billing().add('42', MICRO, 'admin')
        plugin.config['billing_prices'] = 'gpt 1 1'
        self.assertIn('计费暂不可用', plugin._billing_denial(_Event()))


if __name__ == '__main__':
    unittest.main()
