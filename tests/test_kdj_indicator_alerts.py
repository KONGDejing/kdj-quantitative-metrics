from unittest import TestCase
from unittest.mock import MagicMock, patch
from src import runner, notifier


class KdjIndicatorAlertsTests(TestCase):
    def emit(self, code, k, price, stop=None, risk=None, latched=False):
        config = {'alert': {'channels': ['pushplus']},
                  'trade_plan': {'positions': {code: {'stop_loss': stop}}}}
        fake = MagicMock()
        fake.should_alert.return_value = True
        with patch.object(runner, 'state', fake), patch.object(
            runner, 'load_runtime_state', return_value={}
        ), patch.object(runner, 'stop_latched', return_value=latched), patch.object(
            runner, 'notify'
        ) as send:
            runner._maybe_send_kdj_indicator_alert(
                {'code': code, 'name': code},
                {'k': k, 'd': 13, 'j': 0, 'close': price, 'date': '2026-11-04'},
                {'buy': 10, 'sell': 80}, config,
                risk or {'block_new_buys': False}, 600,
            )
            return fake, send, config

    def test_nov4_low_k_is_delivered_despite_previous_stop(self):
        for code, k, price, stop in [('600498', 7.60, 25.31, 26.20),
                                     ('600584', 9.37, 47.93, 63.30)]:
            with self.subTest(code=code):
                fake, send, _ = self.emit(code, k, price, stop)
                send.assert_called_once()
                alert = send.call_args.args[1]
                self.assertTrue(alert['indicator_only'])
                self.assertEqual(alert['direction'], 'low')
                self.assertTrue(alert['entry_cautions'])
                fake.add_alert.assert_called_once_with(alert)

    def test_market_block_missing_stop_and_latched_exit_do_not_hide_k(self):
        _, send, _ = self.emit('600498', 10, 25, risk={
            'block_new_buys': True, 'reasons': ['市场急跌']}, latched=True)
        alert = send.call_args.args[1]
        self.assertEqual(len(alert['entry_cautions']), 3)

    def test_high_signal_still_sends_and_neutral_clears_zone(self):
        _, high, _ = self.emit('600498', 80, 30)
        self.assertEqual(high.call_args.args[1]['direction'], 'high')
        fake, neutral, _ = self.emit('600498', 50, 30)
        neutral.assert_not_called()
        fake.clear_alert_zone.assert_called_once_with('600498:1d_est')

    def test_notification_contains_k_and_restrictions_without_buy_instruction(self):
        _, send, config = self.emit('600498', 7.6, 25.31, 26.2)
        alert = send.call_args.args[1]
        with patch.object(notifier, 'send_pushplus', return_value=True) as push:
            notifier.notify(config, alert)
        content = push.call_args.args[2]
        self.assertIn('K：7.60', content)
        self.assertIn('不是系统买入指令', content)
        self.assertIn('买入检查提示', content)
        self.assertTrue(alert['wechat_sent'])
