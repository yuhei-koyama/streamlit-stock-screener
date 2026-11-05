"""Viewer for saved runs only; opening the page never downloads or runs tests."""
import io
import json
import zipfile

import pandas as pd
import streamlit as st

st.set_page_config(page_title='バックテスト比較',layout='wide')
st.title('バックテスト比較')
st.caption('検索と同じ判定関数を使った7設定と各出口条件の保存結果を確認します。画面を開いても取得やバックテストは実行しません。')
st.info('探索モードは調整済み価格による仮想取引、厳密モードは当時の名目価格・単元を使います。保存結果には区分と設定が記録されています。')
upload=st.file_uploader('保存結果ZIP（run.json・summary.csvと各条件の結果フォルダー）',type=['zip'])
if upload is not None:
    try:
        with zipfile.ZipFile(upload) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80_000_000:
                raise ValueError('展開後80MB以下の結果を指定してください')
            manifest=json.loads(archive.read('run.json'))
            if manifest.get('status') != 'completed' or manifest.get('completed_conditions') != 14:
                raise ValueError('14条件すべて完了した結果ではありません')
            kind=manifest.get('kind')
            if kind not in ('market','synthetic'):
                raise ValueError('データ区分が不明です')
            if kind=='synthetic':
                st.warning('合成データによる動作確認です。実際の収益性を示すものではありません。')
            else:
                st.warning('固定された現在の70銘柄による探索検証です。将来の利益やオルカン超過を確認した結果ではありません。')
            if manifest.get('execution_mode')=='exploratory':
                if manifest.get('evaluation_method') == 'independent':
                    st.warning('調整済み価格・同額元本の独立仮想取引です。分割・配当は価格調整へ含めています。')
                else:
                    st.warning('調整済み価格・仮想1株単位の探索結果です。配当は価格調整へ含めています。')
            for warning in manifest.get('warnings',[]):
                st.caption(warning)
            st.json(manifest,expanded=False)
            summary=pd.read_csv(io.BytesIO(archive.read('summary.csv')))
            independent = manifest.get('evaluation_method') == 'independent'
            benchmark = manifest.get('comparison_method') == 'all_country_same_dates'
            st.subheader('同額・同期間のオルカン比較' if benchmark else
                         '独立仮想取引の損益率比較' if independent else 'CAGR・リスク比較（条件順）')
            st.dataframe(summary,hide_index=True)
            options=[f'{row.strategy_id}__{row.exit_policy}' for row in summary.itertuples()]
            selected=st.selectbox('比較条件',options)
            chosen=summary.iloc[options.index(selected)]
            if independent:
                including_open=manifest.get('include_open_positions', False)
                mean_return=chosen.all_mean_strategy_return if including_open else chosen.mean_net_return
                st.metric('取引あたり平均損益率',f'{mean_return:.2%}' if pd.notna(mean_return) else '対象なし')
                st.caption('各買いシグナルを同額元本で独立評価し、資金と同時保有数の上限を設けていません。')
                if benchmark:
                    base_return=chosen.all_mean_benchmark_return if including_open else chosen.mean_all_country_return
                    excess_return=chosen.all_mean_excess_return if including_open else chosen.mean_excess_return
                    st.metric('同期間のオルカン平均損益率', f'{base_return:.2%}')
                    st.metric('平均超過損益率', f'{excess_return * 100:+.2f}ポイント')
                    if including_open:
                        st.caption('決済済みと期末時価評価を含む全購入取引を表示しています。')
                    st.caption('購入日・売却日の日次基準価額による参考比較です。')
                details=[('銘柄別','by_ticker'),('購入年別','by_entry_year'),
                         ('個別株・ETFの内訳','by_asset_class'),('売買履歴','trades'),
                         ('見送り注文','rejections'),('期末未決済建玉','positions')]
                if including_open:
                    details.insert(0, ('期末評価を含む全取引', 'all_trades'))
            else:
                cagr=chosen.cagr
                st.metric('CAGR',f'{cagr:.2%}' if pd.notna(cagr) else '算出不可')
                daily=pd.read_csv(io.BytesIO(archive.read(f'{selected}/daily.csv')),index_col='date',parse_dates=['date'])
                st.subheader('現金・未収込みの資産推移')
                st.line_chart(daily[['nav']])
                st.subheader('ドローダウン')
                st.line_chart((daily.nav/daily.nav.cummax()-1).rename('drawdown'))
                details=[('年次リターン','annual_returns'),('個別株・ETFの内訳','asset_groups'),
                         ('売買履歴','trades'),('見送り注文','rejections'),('期末未決済建玉','positions')]
            for label,filename in details:
                with st.expander(label):
                    content=archive.read(f'{selected}/{filename}.csv')
                    try:
                        st.dataframe(pd.read_csv(io.BytesIO(content)),hide_index=True)
                    except pd.errors.EmptyDataError:
                        st.caption('該当なし')
    except (ValueError,KeyError,zipfile.BadZipFile,UnicodeDecodeError,pd.errors.ParserError) as exc:
        st.error(f'結果を読み込めません: {exc}')
