"""検索設定と端末識別子をブラウザー内に保存するUI。"""
import streamlit.components.v2 as components


PROFILES = components.component(
    'screening_browser_profiles',
    html='''<div class="profiles"><select aria-label="保存した検索設定"></select>
    <button class="restore">呼び出す</button><button class="remove">削除</button>
    <input aria-label="保存名" placeholder="設定の保存名" maxlength="60">
    <button class="save">現在の条件を保存</button><span role="status"></span></div>''',
    css='''.profiles{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font:14px sans-serif}
    button,input,select{padding:8px;border:1px solid #cbd5e1;border-radius:6px;background:white;color:#172b45}
    button{cursor:pointer}span{font-size:12px;color:#52657b}''',
    js='''export default function({data,parentElement,setStateValue,setTriggerValue}) {
      const key='jp-screening-profiles-v1', clientKey='jp-screening-client-v1';
      const root=parentElement.querySelector('.profiles');
      const select=root.querySelector('select'), status=root.querySelector('span');
      let profiles={};
      try {
        profiles=JSON.parse(localStorage.getItem(key)||'{}');
        if (!profiles || Array.isArray(profiles) || typeof profiles!=='object') profiles={};
        let client=localStorage.getItem(clientKey);
        if (!client) {client=crypto.randomUUID();localStorage.setItem(clientKey,client)}
        setStateValue('client',client);
      } catch(e) {status.textContent='ブラウザー保存を利用できません'}
      function choices() {
        select.replaceChildren();
        const blank=document.createElement('option');blank.value='';blank.textContent='保存した設定';select.append(blank);
        Object.keys(profiles).sort().forEach(name=>{
          const option=document.createElement('option');option.value=name;option.textContent=name;select.append(option);
        });
      }
      choices();
      root.querySelector('.save').onclick=()=>{
        const name=root.querySelector('input').value.trim();
        if(!name){status.textContent='保存名を入力してください';return}
        try{profiles[name]=data.settings;localStorage.setItem(key,JSON.stringify(profiles));choices();select.value=name;status.textContent='このブラウザーに保存しました'}
        catch(e){status.textContent='保存できませんでした'}
      };
      root.querySelector('.restore').onclick=()=>{
        if(profiles[select.value]) setTriggerValue('restore',{settings:profiles[select.value]});
      };
      root.querySelector('.remove').onclick=()=>{
        if(!select.value)return;
        try{delete profiles[select.value];localStorage.setItem(key,JSON.stringify(profiles));choices();status.textContent='削除しました'}
        catch(e){status.textContent='削除できませんでした'}
      };
      const media=window.matchMedia('(max-width: 760px)');
      const resize=()=>setStateValue('mobile',media.matches);resize();media.addEventListener('change',resize);
      return ()=>media.removeEventListener('change',resize);
    }''')
