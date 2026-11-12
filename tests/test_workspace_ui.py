from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from test_screening_workspace import prices


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceUITests(unittest.TestCase):
    def test_initial_values_and_no_initial_fetch(self):
        with patch('stock_screener.workspace_ui.DailyStore') as store:
            store.return_value.root = ROOT/'outputs'/'test_ui'
            screen = AppTest.from_file(str(ROOT/'app.py'),default_timeout=20).run()
            self.assertEqual(len(screen.exception),0)
            self.assertEqual(screen.number_input(key='search_turnover').value,10.)
            self.assertEqual(screen.number_input(key='search_limit').value,100)
            self.assertEqual(screen.number_input(key='search_commission').value,0.)
            self.assertEqual(screen.number_input(key='search_slippage').value,.05)
            self.assertEqual(screen.selectbox(key='search_stop').value,10.)
            self.assertFalse(screen.checkbox(key='search_budget_enabled').value)
            self.assertTrue(screen.checkbox(key='search_liquidity_enabled').value)
            screen.checkbox(key='search_liquidity_enabled').uncheck().run()
            self.assertTrue(screen.number_input(key='search_turnover').disabled)
            self.assertTrue(screen.number_input(key='search_limit').disabled)
            self.assertEqual(screen.number_input(key='search_turnover').value,10.)
            self.assertFalse(screen.checkbox(key='search_budget_enabled').value)
            store.return_value.refresh.assert_not_called()

    def test_custom_search_detail_and_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Mock()
            store.root = Path(temporary)
            store.read.return_value = prices()
            store.refresh.return_value = (prices(),False)
            with patch('stock_screener.workspace_ui.DailyStore',return_value=store):
                screen = AppTest.from_file(str(ROOT/'app.py'),default_timeout=25).run()
                screen.session_state['search_scope']='コード指定'
                screen.session_state['search_codes']='7203,1306'
                screen.session_state['search_turnover']=0.
                screen.run()
                next(b for b in screen.button if b.label=='この条件で検索').click().run()
                self.assertEqual(len(screen.exception),0)
                self.assertEqual(len(screen.session_state['search_output']['records']),2)
                screen.multiselect(key='result_states').set_value(['成立','不成立','判定不可']).run()
                next(b for b in screen.button if b.label=='選択した銘柄を開く').click().run()
                self.assertEqual(len(screen.exception),0)
                self.assertEqual(screen.title[0].value,'銘柄詳細')
                next(b for b in screen.button if b.label=='検索結果へ戻る').click().run()
                self.assertEqual(len(screen.exception),0)
                self.assertEqual(screen.title[0].value,'銘柄検索')
                self.assertEqual(screen.number_input(key='search_turnover').value,0.)
                self.assertEqual(screen.session_state['search_scope'],'コード指定')
                self.assertEqual(len(screen.session_state['search_output']['records']),2)


if __name__=='__main__':
    unittest.main()
