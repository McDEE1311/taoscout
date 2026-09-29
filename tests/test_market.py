import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from market.engine import Candle, HOUR, Rules, condition, dataset_hash, read_csv, replay, validate
from market.ledger import connect, history, publish
from market.__main__ import audit_snapshots

START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def candles(n=240, rising=True):
    result = []
    for i in range(n):
        value = 100 + i if rising else 1000 - i
        result.append(Candle(START+i*HOUR, START+(i+1)*HOUR, value, value+2, value-2, value+1))
    return result


class EngineTests(unittest.TestCase):
    def test_future_candles_cannot_change_previous_decision(self):
        bars = candles()
        at = START + 96*HOUR
        original = condition(bars, at)
        changed = bars[:96] + [replace(b, open=9000, high=9002, low=8998, close=9001) for b in bars[96:]]
        self.assertEqual(original, condition(changed, at))
        self.assertEqual(original, condition(bars[:96], at))
        self.assertEqual(original['condition'], 'bullish')
        self.assertIsNone(original['confidence'])

    def test_late_data_is_not_known_at_decision_time(self):
        bars = candles()
        bars[95] = replace(bars[95], available_at=START+97*HOUR)
        self.assertEqual(condition(bars, START+96*HOUR)['condition'], 'no_clear_signal')

    def test_stale_history_does_not_make_current_signal(self):
        self.assertEqual(condition(candles(96), START+98*HOUR)['condition'], 'no_clear_signal')

    def test_gap_duplicate_unsorted_nan_bad_ohlc_rejected(self):
        original = candles(5)
        variants = [original[:2]+original[3:], original+[original[-1]], list(reversed(original)),
                    [replace(original[0], close=float('nan'))],
                    [replace(original[0], close=10000)],
                    [replace(original[0], available_at=START)],
                    [replace(original[0], start=START+timedelta(minutes=1))]]
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate(bad)

    def test_realistic_execution_delay_and_both_side_costs(self):
        bars = candles(100)
        result = replay(bars, (START+72*HOUR).isoformat(), 'synthetic test fixture')
        trade = result['trades'][0]
        self.assertEqual(trade['signal_time'], (START+72*HOUR).isoformat())
        self.assertEqual(trade['entry_at'], (START+73*HOUR).isoformat())
        expected = (1-.002)**2 * bars[-1].close/bars[73].open - 1
        self.assertAlmostEqual(result['net_return'], expected)
        expected_hold = (1-.002)**2 * bars[-1].close/bars[72].open - 1
        self.assertAlmostEqual(result['buy_hold_net_return'], expected_hold)
        self.assertEqual(result['closed_trades'], 1)

    def test_bearish_is_cash_not_a_short(self):
        result = replay(candles(rising=False), (START+72*HOUR).isoformat(), 'synthetic')
        self.assertEqual(result['closed_trades'], 0)
        self.assertEqual(result['net_return'], 0)
        self.assertIsNone(result['win_rate'])
        self.assertLess(result['buy_hold_net_return'], 0)

    def test_losing_roundtrip_is_retained(self):
        bars = candles(160)
        for i in range(90,160):
            value = 120-(i-90)*.5
            bars[i] = replace(bars[i], open=value, close=value, high=value+1, low=value-1)
        result = replay(bars, (START+72*HOUR).isoformat(), 'synthetic')
        self.assertTrue(any(t['net_return'] < 0 for t in result['trades']))
        self.assertGreater(result['max_drawdown'], 0)
        self.assertEqual(result['closed_trades'], len(result['trades']))

    def test_warmup_and_cost_guards(self):
        with self.assertRaises(ValueError): replay(candles(), START.isoformat(), 'synthetic')
        for cost in (-1, float('nan'), float('inf'),1001):
            with self.assertRaises(ValueError): Rules(fee_bps=cost)

    def test_replay_prefix_decisions_match(self):
        bars = candles()
        full = replay(bars, (START+72*HOUR).isoformat(), 'synthetic')
        partial = replay(bars[:120], (START+72*HOUR).isoformat(), 'synthetic')
        self.assertEqual(partial['signals'], full['signals'][:len(partial['signals'])])

    def test_hash_covers_input_and_availability(self):
        bars = candles(5)
        changed = [replace(bars[0], available_at=START+2*HOUR)] + bars[1:]
        self.assertNotEqual(dataset_hash(bars), dataset_hash(changed))

    def test_csv_requires_timezone_and_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'input.csv'
            path.write_text('start,open,high,low,close\n2025-01-01,1,1,1,1\n')
            with self.assertRaises(ValueError): read_csv(path)
            with path.open('w') as handle:
                writer = csv.DictWriter(handle, fieldnames=['start','available_at','open','high','low','close'])
                writer.writeheader()
                writer.writerows(b.record() for b in candles(5))
            self.assertEqual(read_csv(path), candles(5))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'research.db'
        self.now = START + 96*HOUR + timedelta(minutes=5)

    def tearDown(self): self.tmp.cleanup()

    def test_publication_is_idempotent(self):
        first, created = publish(self.path, candles(), 'synthetic', self.now)
        second, repeated = publish(self.path, candles(), 'changed source', self.now+timedelta(minutes=1))
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first, second)
        self.assertEqual(len(history(self.path)['records']), 1)

    def test_outside_slot_rejected(self):
        with self.assertRaises(ValueError): publish(self.path, candles(), 'synthetic', self.now+HOUR)
        self.assertFalse(self.path.exists())

    def test_no_history_reader_side_effects(self):
        self.assertEqual(history(self.path)['integrity'], 'empty')
        self.assertFalse(self.path.exists())

    def test_publication_does_not_hash_future_inputs(self):
        record, _ = publish(self.path, candles(), 'synthetic', self.now)
        self.assertEqual(record['input_sha256'], dataset_hash(candles(96)))

    def test_sql_update_delete_are_blocked(self):
        publish(self.path, candles(), 'synthetic', self.now)
        conn = connect(self.path)
        try:
            for sql in ('UPDATE research SET payload=\'{}\'', 'DELETE FROM research'):
                with self.assertRaises(sqlite3.IntegrityError): conn.execute(sql)
                conn.rollback()
        finally: conn.close()

    def test_tampering_is_detected(self):
        publish(self.path, candles(), 'synthetic', self.now)
        conn = sqlite3.connect(self.path)
        conn.execute('DROP TRIGGER research_no_update')
        conn.execute("UPDATE research SET payload='{}'")
        conn.commit(); conn.close()
        with self.assertRaises(ValueError): history(self.path)

    def test_pagination_contains_all_records(self):
        for i in range(4): publish(self.path, candles(), 'synthetic', self.now+i*8*HOUR)
        page = history(self.path, limit=2)
        next_page = history(self.path, limit=2, before=page['next_before'])
        self.assertEqual([r['id'] for r in page['records']+next_page['records']], [4,3,2,1])
        self.assertIsNone(next_page['next_before'])

    def test_legacy_db_is_audited_read_only(self):
        legacy = Path(self.tmp.name)/'legacy.db'
        conn = sqlite3.connect(legacy)
        conn.execute('CREATE TABLE snapshots(fetched_at TEXT, tao_price_usd REAL)')
        conn.execute("INSERT INTO snapshots VALUES ('2025-01-01T00:00:00+00:00', 100)")
        conn.commit(); conn.close()
        original = legacy.read_bytes()
        result = audit_snapshots(legacy)
        self.assertFalse(result['eligible_for_price_backtest'])
        self.assertEqual(result['positive_price_rows'],1)
        self.assertEqual(original, legacy.read_bytes())


if __name__ == '__main__': unittest.main()
