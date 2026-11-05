"""Minimal authored fixtures for a real KudaGO overnight schedule and skip reports."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from event_collector.kudago_collect import write_skip_report
from event_collector.kudago_html import normalize
from event_collector.source_http import SyncError

URL = 'https://kudago.com/msk/event/test-night-show/'
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def night_card(day='28–29 ноября', clock='23:30–3:00', *, category='concerts', **changes):
    event = {'@type':'Event', 'url':URL, 'name':'Test night concert',
             'startDate':'2026-11-28T23:30:00', 'endDate':'2026-11-29T03:00:00',
             'eventStatus':'https://schema.org/EventScheduled'}
    event.update(changes)
    return (f'<script type="application/ld+json">{json.dumps(event)}</script>'
            f'<a class="crumbs-list-item-link" href="/msk/{category}/">Category</a>'
            '<span data-type-name="event" data-id="123" data-classnames="post-fav-top"></span>'
            '<div class="converting-details"><div class="post-big-detail"><big>Место проведения</big>'
            '<span class="post-big-detail-stub">Test hall</span></div><span class="addressItem">Test street 1</span></div>'
            f'<table class="post-big-details-schedule"><tr><td>{day}</td><td>{clock}</td></tr></table>')


def parse(html, now=NOW):
    return normalize(html, URL, now, now + timedelta(days=60))


class KudaDiagnosticsContracts(unittest.TestCase):
    def test_one_night_produces_one_start_in_moscow_time(self):
        for day, clock in [('28–29 ноября','23:30–3:00'), ('28 - 29 ноября 2026','23:30 - 03:00')]:
            with self.subTest(day=day):
                record, reason = parse(night_card(day, clock))
                self.assertIsNone(reason)
                self.assertEqual(record['occurrences'], [{'id':'t1795897800',
                    'starts_at':'2026-11-28T20:30:00+00:00', 'timezone':'Europe/Moscow', 'status':'scheduled'}])
                self.assertIsNone(record['period'])

    def test_ambiguous_ranges_opening_hours_and_stale_sessions_stay_rejected(self):
        for day, clock in [('28–30 ноября','23:30–3:00'), ('28–29 ноября','10:00–18:00'),
                           ('28–29 ноября','23:30–23:30'), ('28 ноября','23:30–3:00'),
                           ('28–29 ноября пт, сб','23:30–3:00'), ('28–29 ноября 2025','23:30–3:00')]:
            with self.subTest(day=day, clock=clock):
                self.assertIsNone(parse(night_card(day, clock))[0])
        for kwargs in [{'endDate':'2026-11-29T02:00:00'},
                       {'endDate':'2025-11-29T03:00:00'},
                       {'eventStatus':'https://schema.org/EventCancelled'}]:
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(parse(night_card(**kwargs))[0])
        self.assertIsNone(parse(night_card(), datetime(2026,11,28,21,tzinfo=timezone.utc))[0])
        # A visiting window is still an estimated period, never an invented showtime.
        art, _ = parse(night_card(category='exhibitions'))
        self.assertEqual(art['occurrences'], [])
        self.assertEqual(art['period']['precision'], 'estimated')

    def test_overnight_must_fit_real_calendar_and_horizon(self):
        self.assertIsNone(parse(night_card('30–31 ноября', '23:30–3:00',
            startDate='2026-11-01T00:00:00', endDate='2026-12-02T00:00:00'))[0])
        self.assertIsNone(parse(night_card(), datetime(2026,8,1,tzinfo=timezone.utc))[0])

    def test_skip_report_is_bounded_allowlisted_and_not_a_publication_receipt(self):
        row = dict(url=URL, category='music', reason='no_unambiguous_venue')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'reports/skips.json'
            write_skip_report(path,[row],complete=False,fetched=2,accepted=1)
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(report['collection_complete'])
            self.assertEqual(report['scope'],'card_extraction')
            self.assertEqual(report['reasons'], {'no_unambiguous_venue':1})
            self.assertEqual(report['skipped_cards'], [row])
            self.assertNotIn('published',report)
            for bad in [{**row,'url':URL+'?token=secret'}, {**row,'reason':'private response'},
                        {**row,'html':'private'}, {**row,'url':'https://other.test/'}]:
                with self.assertRaises(SyncError):
                    write_skip_report(path,[bad],complete=True,fetched=2,accepted=1)
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), report)
            with self.assertRaises(SyncError):
                write_skip_report(path,[row]*601,complete=True,fetched=601,accepted=0)
            write_skip_report(path,[],complete=True,fetched=3,accepted=3)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['skipped_cards'], [])
            self.assertFalse(path.with_suffix('.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
