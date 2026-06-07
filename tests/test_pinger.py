import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pinger


class DoPingTests(unittest.TestCase):
    def run_ping_with_stdout(self, stdout, count=3):
        with patch('pinger.subprocess.run', return_value=SimpleNamespace(stdout=stdout)) as run:
            result = pinger.do_ping('example.com', count)
            run.assert_called_once_with(
                ['ping', '-n', '-D', '-c', str(count), '-i', '0.3', '-W', '2', '--', 'example.com'],
                capture_output=True,
                text=True,
                timeout=count * 3 + 2,
            )
            return result

    def test_successful_replies_compute_latency_from_reply_lines(self):
        result = self.run_ping_with_stdout(
            '''PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
[1710000000.000001] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=12.3 ms
[1710000000.300001] 64 bytes from 8.8.8.8: icmp_seq=2 ttl=117 time=10.0 ms
[1710000000.600001] 64 bytes from 8.8.8.8: icmp_seq=3 ttl=117 time=18.6 ms

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 602ms
rtt min/avg/max/mdev = 10.000/13.633/18.600/3.559 ms
'''
        )

        self.assertEqual((result[0], result[2], result[3], result[4], result[5]), (10.0, 18.6, 0.0, 3, 3))
        self.assertAlmostEqual(result[1], 13.633333333333333)

    def test_partial_loss_uses_received_reply_latencies(self):
        result = self.run_ping_with_stdout(
            '''PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
[1710000000.000001] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=20.0 ms
[1710000000.600001] 64 bytes from 8.8.8.8: icmp_seq=3 ttl=117 time=30.0 ms

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 2 received, 33.3333% packet loss, time 602ms
rtt min/avg/max/mdev = 20.000/25.000/30.000/5.000 ms
'''
        )

        self.assertEqual(result, (20.0, 25.0, 30.0, 33.3333, 3, 2))

    def test_total_loss_returns_null_latencies(self):
        result = self.run_ping_with_stdout(
            '''PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2047ms
'''
        )

        self.assertEqual(result, (None, None, None, 100.0, 3, 0))

    def test_missing_summary_derives_received_from_reply_count(self):
        result = self.run_ping_with_stdout(
            '''[1710000000.000001] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=1.5 ms
[1710000000.300001] 64 bytes from 8.8.8.8: icmp_seq=2 ttl=117 time=2.5 ms
''',
            count=4,
        )

        self.assertEqual(result, (1.5, 2.0, 2.5, 50.0, 4, 2))

    def test_timeout_returns_total_loss(self):
        with patch('pinger.subprocess.run', side_effect=subprocess.TimeoutExpired(['ping'], 11)):
            result = pinger.do_ping('example.com', 3)

        self.assertEqual(result, (None, None, None, 100.0, 3, 0))


if __name__ == '__main__':
    unittest.main()
