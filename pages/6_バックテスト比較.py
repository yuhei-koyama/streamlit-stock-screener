"""Viewer for saved runs only; opening the page never downloads or runs tests."""
import io
import json
import zipfile

import pandas as pd
import streamlit as st

st.set_page_config(page_title='バックテスト比較',layout='wide')
st.title('バックテスト比較')
st.caption('検索と同じ判定関数を使った固定7設定×出口A/Bの保存結果を確認します。画面を開いても取得やバックテストは実行しません。')
st.info('実データ検証には、当時の名目価格・単元・企業行動データの確認が必要です。保存結果には使用データと設定が記録されています。')
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
            for warning in manifest.get('warnings',[]):
                st.caption(warning)
            st.json(manifest,expanded=False)
            summary=pd.read_csv(io.BytesIO(archive.read('summary.csv')))
            st.subheader('CAGR・リスク比較（条件順、収益順ランキングではありません）')
            st.dataframe(summary,hide_index=True)
            options=[f'{row.strategy_id}__{row.exit_policy}' for row in summary.itertuples()]
            selected=st.selectbox('比較条件',options)
            chosen=summary.iloc[options.index(selected)]
            cagr=chosen.cagr
            st.metric('CAGR',f'{cagr:.2%}' if pd.notna(cagr) else '算出不可')
            daily=pd.read_csv(io.BytesIO(archive.read(f'{selected}/daily.csv')),index_col='date',parse_dates=['date'])
            st.subheader('現金・未収込みの資産推移')
            st.line_chart(daily[['nav']])
            st.subheader('ドローダウン')
            st.line_chart((daily.nav/daily.nav.cummax()-1).rename('drawdown'))
            for label,filename in [('年次リターン','annual_returns'),('個別株・ETFの内訳','asset_groups'),
                                    ('売買履歴','trades'),('見送り注文','rejections'),('期末未決済建玉','positions')]:
                with st.expander(label):
                    content=archive.read(f'{selected}/{filename}.csv')
                    try:
                        st.dataframe(pd.read_csv(io.BytesIO(content)),hide_index=True)
                    except pd.errors.EmptyDataError:
                        st.caption('該当なし')
    except (ValueError,KeyError,zipfile.BadZipFile,UnicodeDecodeError,pd.errors.ParserError) as exc:
        st.error(f'結果を読み込めません: {exc}')
