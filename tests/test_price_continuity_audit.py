import unittest
import pandas as pd
from scripts.audit_price_continuity import scan_prices, interval_overlaps


def prices(dates, closes, volumes):
    frame=pd.DataFrame({'Open':closes,'High':closes,'Low':closes,'Close':closes,'Volume':volumes},index=dates)
    frame.index.name='Date'
    return frame


class PriceContinuityAuditTests(unittest.TestCase):
    def test_zero_volume_extreme_price_uses_both_trade_anchors(self):
        dates=pd.bdate_range('2021-01-04',periods=5)
        frame=prices(dates,[100.,101.,1e10,1e10,102.],[1000,1000,0,0,1000])
        zero,events,invalid=scan_prices(frame,dates)
        self.assertEqual(len(invalid),0)
        self.assertEqual(len(zero),2)
        self.assertTrue(zero.review_candidate.all())
        suspected=events[events.event_type.eq('zero_volume_price_dislocation')]
        self.assertEqual(len(suspected),2)
        self.assertTrue(suspected.priority.eq('extreme_scale').all())
        self.assertTrue(zero.next_tradable_close.eq(102.).all())

    def test_flat_zero_volume_is_recorded_without_price_dislocation(self):
        dates=pd.bdate_range('2021-01-04',periods=4)
        zero,events,invalid=scan_prices(prices(dates,[100.]*4,[10,0,0,10]),dates)
        self.assertEqual(len(zero),2)
        self.assertFalse(zero.review_candidate.any())
        self.assertTrue(events.empty)
        self.assertTrue(invalid.empty)

    def test_calendar_session_gap_is_not_calendar_day_gap(self):
        dates=pd.bdate_range('2021-01-04',periods=30)
        zero,events,invalid=scan_prices(prices(dates[[0,1,25,26]],[100.]*4,[10]*4),dates)
        gaps=events[events.event_type.eq('missing_price_rows')]
        self.assertEqual(len(gaps),1)
        self.assertEqual(int(gaps.iloc[0].missing_sessions),23)
        self.assertEqual(gaps.iloc[0].start_date,dates[2])
        self.assertEqual(gaps.iloc[0].end_date,dates[25])
        self.assertEqual(len(events[events.event_type.eq('no_positive_volume_rows')]),1)

    def test_terminal_zero_volume_run_and_all_zero_history(self):
        dates=pd.bdate_range('2021-01-04',periods=25)
        _,events,_=scan_prices(prices(dates,[100.]*25,[10]+[0]*24),dates)
        self.assertEqual(len(events[events.event_type.eq('trailing_no_positive_volume')]),1)
        _,events,_=scan_prices(prices(dates,[100.]*25,[0]*25),dates)
        self.assertEqual(len(events[events.event_type.eq('no_positive_volume_history')]),1)

    def test_leading_zero_volume_history_is_recorded_as_observed_interval(self):
        dates=pd.bdate_range('2021-01-04',periods=25)
        _,events,_=scan_prices(prices(dates,[100.]*25,[0]*24+[10]),dates)
        leading=events[events.event_type.eq('leading_no_positive_volume')]
        self.assertEqual(len(leading),1)
        self.assertEqual(leading.iloc[0].start_date,dates[0])
        self.assertEqual(leading.iloc[0].end_date,dates[-1])
        self.assertEqual(leading.iloc[0].missing_sessions,24)

    def test_price_jump_with_positive_volume_is_review_candidate(self):
        dates=pd.bdate_range('2021-01-04',periods=3)
        zero,events,invalid=scan_prices(prices(dates,[100.,200.,100.],[10]*3),dates)
        self.assertEqual(len(events[events.event_type.eq('close_discontinuity')]),2)
        self.assertTrue(zero.empty)
        self.assertTrue(invalid.empty)

    def test_invalid_calendar_and_invalid_ohlc_are_distinct(self):
        dates=pd.bdate_range('2021-01-04',periods=3)
        frame=prices(dates,[100.]*3,[10]*3)
        frame.loc[dates[1],'High']=99.
        _,_,invalid=scan_prices(frame,dates)
        self.assertEqual(len(invalid),1)
        with self.assertRaises(ValueError):
            scan_prices(frame,dates[:2])

    def test_holding_overlap_includes_exit_day_and_gap_interval(self):
        trades=pd.DataFrame([dict(ticker='1111.T',trade_id='t',entry_date=pd.Timestamp('2021-01-05'),
                                exit_date=pd.Timestamp('2021-01-08'),position_status='closed')])
        events=pd.DataFrame([dict(ticker='1111.T',event_id='before',start_date=pd.Timestamp('2021-01-04'),
                                 end_date=pd.Timestamp('2021-01-04'),event_type='point',priority='review'),
                             dict(ticker='1111.T',event_id='exit',start_date=pd.Timestamp('2021-01-08'),
                                 end_date=pd.Timestamp('2021-01-08'),event_type='point',priority='review'),
                             dict(ticker='1111.T',event_id='gap',start_date=pd.Timestamp('2021-01-01'),
                                 end_date=pd.Timestamp('2021-01-20'),event_type='gap',priority='review')])
        found=interval_overlaps(trades,events)
        self.assertEqual({row['event_id'] for row in found},{'exit','gap'})


if __name__=='__main__':
    unittest.main()