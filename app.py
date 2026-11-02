from __future__ import annotations
import html
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import quote
import pandas as pd
import requests
import streamlit as st
import folium
from branca.element import MacroElement, Template
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

BASE=Path(__file__).resolve().parent
st.set_page_config(page_title='ГРАНЕЛЬ — карта объектов',page_icon='🏙️',layout='wide')
st.markdown('''<style>.block-container{padding-top:2rem}h1{color:#183d38}div[data-testid="stMetric"]{background:#f0f5f2;padding:18px;border-radius:12px}.stButton>button{border-radius:8px}</style>''',unsafe_allow_html=True)

def key():
    try: return str(st.secrets.get('DGIS_API_KEY','')) or os.getenv('DGIS_API_KEY','')
    except Exception: return os.getenv('DGIS_API_KEY','')

def read_objects(source):
    df=pd.read_excel(source,engine='openpyxl',dtype={'ID':str,'Телефон':str,'НЖП':str})
    required=['ID','Объект','Адрес','ЖК','Ответственный','Телефон','Широта','Долгота']
    missing=set(required)-set(df.columns)
    if missing: raise ValueError('Не хватает столбцов: '+', '.join(sorted(missing)))
    if df['ID'].isna().any() or df['ID'].duplicated().any(): raise ValueError('ID должны быть заполнены и уникальны.')
    if df['Адрес'].isna().any() or df['ЖК'].isna().any(): raise ValueError('Заполните адрес и ЖК каждой строки.')
    for c in ['Широта','Долгота']: df[c]=pd.to_numeric(df[c],errors='coerce')
    return df

def valid(df):
    return df['Широта'].between(-90,90)&df['Долгота'].between(-180,180)

def txt(v):
    if pd.isna(v): return '—'
    if isinstance(v,pd.Timestamp): return v.strftime('%d.%m.%Y')
    return str(v)

def api_error(response):
    if response.status_code in (401,403): raise ValueError('2ГИС: проверьте ключ и доступ к нужному API.')
    if response.status_code==429: raise ValueError('2ГИС: исчерпан лимит запросов. Повторите позже.')
    if not response.ok: raise ValueError(f'2ГИС: ошибка HTTP {response.status_code}.')
    data=response.json()
    if isinstance(data,dict) and data.get('meta',{}).get('code',200)!=200:
        raise ValueError('2ГИС не выполнил запрос. Проверьте доступ и лимит сервиса.')
    return data

@st.cache_data(ttl=86400,show_spinner=False)
def geocode(address,api_key):
    if not api_key: raise ValueError('Укажите ключ 2ГИС.')
    try:
        r=requests.get('https://catalog.api.2gis.com/3.0/items/geocode',params={'q':address,'fields':'items.point,items.address','key':api_key,'page_size':5},timeout=15)
        data=api_error(r)
    except requests.RequestException: raise ValueError('Нет ответа от геокодера 2ГИС. Проверьте подключение.') from None
    items=[i for i in data.get('result',{}).get('items',[]) if i.get('point') and i.get('type')=='building']
    if len(items)!=1: raise ValueError('Адрес не найден однозначно. Уточните адрес или внесите координаты вручную.')
    item=items[0]
    return {'lat':item['point']['lat'],'lon':item['point']['lon'],'name':item.get('full_name',item.get('name',address))}

def popup(row):
    body=''.join(f'<b>{html.escape(c)}:</b> {html.escape(txt(v))}<br>' for c,v in row.items() if c not in ['Широта','Долгота'] and pd.notna(v) and str(v).strip())
    return folium.Popup('<div style="width:340px;max-height:440px;overflow:auto;font:13px/1.6 Arial">'+body+'</div>',max_width=380)

def map_view(df,api_key,route=None):
    located=df[valid(df)]
    m=folium.Map(location=[55.75,37.62],zoom_start=9,tiles=None,control_scale=True)
    if api_key:
        folium.TileLayer('https://tile0.maps.2gis.com/v2/tiles/online_hd/{z}/{x}/{y}.png?key='+quote(api_key,safe=''),attr='© 2ГИС',name='2ГИС',max_zoom=20).add_to(m)
    else:
        st.info('Для подложки карты 2ГИС укажите ключ в настройках. Реестр доступен без ключа.')
    overview=folium.FeatureGroup(name='Жилые комплексы').add_to(m)
    houses=folium.FeatureGroup(name='Дома').add_to(m)
    for name,part in located.groupby('ЖК'):
        total=int((df['ЖК']==name).sum())
        label=html.escape(str(name))
        folium.Marker([part['Широта'].mean(),part['Долгота'].mean()],tooltip=f'{name}: {total} домов в выборке, {len(part)} на карте',icon=folium.DivIcon(html=f'<div style="background:#183d38;color:white;border:2px solid white;border-radius:10px;padding:7px;width:155px;text-align:center;font:12px Arial;box-shadow:0 2px 7px #777">{label}<br><b>{total} домов</b></div>',icon_size=(170,55))).add_to(overview)
        cluster=MarkerCluster(name=str(name),control=False,disableClusteringAtZoom=16).add_to(houses)
        for _,r in part.iterrows(): folium.Marker([r['Широта'],r['Долгота']],tooltip=html.escape(str(r['Объект'])),popup=popup(r),icon=folium.Icon(color='darkgreen',icon='home')).add_to(cluster)
    if not located.empty: m.fit_bounds(located[['Широта','Долгота']].values.tolist(),max_zoom=16)
    toggle=MacroElement()
    toggle._template=Template('''{% macro script(this, kwargs) %}
    var map={{this._parent.get_name()}}, overview={{this.overview}}, houses={{this.houses}};
    function switchScale(){if(map.getZoom()<13){map.addLayer(overview);map.removeLayer(houses);}else{map.removeLayer(overview);map.addLayer(houses);}}
    map.on('zoomend',switchScale);switchScale();
    {% endmacro %}''')
    toggle.overview=overview.get_name();toggle.houses=houses.get_name();m.add_child(toggle)
    if route:
        for label,p in [('А',route['a']),('Б',route['b'])]: folium.Marker([p['lat'],p['lon']],tooltip=label+': '+html.escape(p['name']),icon=folium.Icon(color='red',icon='flag')).add_to(m)
        for segment in route.get('segments',[]): folium.PolyLine(segment,color='#df633e',weight=5).add_to(m)
        points=[p for seg in route.get('segments',[]) for p in seg] or [[route[p]['lat'],route[p]['lon']] for p in ['a','b']]
        m.fit_bounds(points,max_zoom=16)
    return m

DGIS_ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
DGIS_TRANSIT_URL = "https://routing.api.2gis.com/public_transport/2.0"

def wkt_line(value: str) -> list[list[float]]:
    match = re.fullmatch(r"LINESTRING\s*\((.+)\)", value.strip(), re.IGNORECASE)
    if not match:
        return []
    try:
        points = []
        for pair in match.group(1).split(","):
            lon, lat = map(float, pair.strip().split()[:2])
            points.append([lat, lon])
        return points if len(points) >= 2 else []
    except (ValueError, IndexError):
        return []

def dgis_route(
    origin_lat: float,
    origin_lon: float,
    destination_lat: float,
    destination_lon: float,
    travel_mode: str,
    api_key: str,
) -> dict[str, Any] | None:
    if not api_key:
        return None

    if travel_mode == "Общественный транспорт":
        url = DGIS_TRANSIT_URL
        body = {
            "source": {"point": {"lat": origin_lat, "lon": origin_lon}},
            "target": {"point": {"lat": destination_lat, "lon": destination_lon}},
            "transport": ["pedestrian", "metro", "light_metro", "mcc", "mcd",
                          "suburban_train", "tram", "bus", "trolleybus", "shuttle_bus"],
            "locale": "ru",
        }
    else:
        url = DGIS_ROUTING_URL
        point_type = "walking" if travel_mode == "Пешком" else "stop"
        body = {
            "points": [
                {"type": point_type, "lon": origin_lon, "lat": origin_lat},
                {"type": point_type, "lon": destination_lon, "lat": destination_lat},
            ],
            "transport": "walking" if travel_mode == "Пешком" else "driving",
            "route_mode": "fastest",
            "output": "detailed",
            "locale": "ru",
        }

    response = requests.post(url, params={"key": api_key}, json=body, timeout=18)
    response.raise_for_status()
    payload = response.json()
    variants = payload if isinstance(payload, list) else payload.get("result", [])
    if not isinstance(variants, list):
        return None
    variants = [
        item for item in variants
        if isinstance(item, dict) and item.get("total_duration") is not None
        and (travel_mode != "Общественный транспорт" or not item.get("pedestrian"))
    ]
    if not variants:
        return None
    route = min(variants, key=lambda item: item["total_duration"])
    selections = []
    if travel_mode == "Общественный транспорт":
        for movement in route.get("movements", []):
            alternatives = movement.get("alternatives") or []
            if alternatives:
                selections.extend(part.get("selection", "") for part in alternatives[0].get("geometry", []))
    else:
        for end in ("begin_pedestrian_path", "end_pedestrian_path"):
            selections.append((route.get(end) or {}).get("geometry", {}).get("selection", ""))
        for maneuver in route.get("maneuvers", []):
            selections.extend(part.get("selection", "") for part in
                              (maneuver.get("outcoming_path") or {}).get("geometry", []))
    return {
        "minutes": float(route["total_duration"]) / 60,
        "road_km": float(route["total_distance"]) / 1000,
        "segments": [line for selection in selections if (line := wkt_line(selection))],
        "provider": "2ГИС",
        "transfers": route.get("transfer_count") if travel_mode == "Общественный транспорт" else None,
    }

st.title('ГРАНЕЛЬ · Карта объектов')
st.caption('Дома в управлении · ответственные · маршруты между адресами')
api_key=key()
with st.sidebar:
    st.header('Данные и настройки')
    api_key=st.text_input('Ключ 2ГИС для этой сессии',type='password',help='Постоянный ключ задаётся в .streamlit/secrets.toml.') or api_key
    upload=st.file_uploader('Загрузить обновлённый objects.xlsx',type=['xlsx'])
    st.caption('Изменяйте ответственного, телефон и другие данные в Excel. Загрузка применяется к вашей сессии.')
    st.download_button('Скачать исходный Excel',(BASE/'objects.xlsx').read_bytes(),file_name='objects.xlsx')
try: objects=read_objects(io.BytesIO(upload.getvalue()) if upload else BASE/'objects.xlsx')
except Exception as e: st.error('Ошибка Excel: '+str(e));st.stop()
if objects.empty: st.info('В Excel нет объектов. Добавьте строки.');st.stop()
if 'geo_cache' not in st.session_state:
    try: st.session_state.geo_cache=json.loads((BASE/'coordinates.json').read_text())
    except (OSError,ValueError): st.session_state.geo_cache={}
for i,r in objects.iterrows():
    cached=st.session_state.geo_cache.get(str(r['Адрес']))
    if cached and not valid(objects.loc[[i]]).iloc[0]: objects.loc[i,['Широта','Долгота']]=[cached['lat'],cached['lon']]
with st.sidebar:
    if st.button('Найти недостающие координаты',disabled=not api_key):
        unresolved=objects[~valid(objects)]
        progress=st.progress(0);errors=[]
        for n,(i,r) in enumerate(unresolved.iterrows()):
            try:
                found=geocode(str(r['Адрес']),api_key)
                st.session_state.geo_cache[str(r['Адрес'])]=found
                objects.loc[i,['Широта','Долгота']]=[found['lat'],found['lon']]
            except ValueError as e:
                errors.append(f"{r['Адрес']}: {e}")
                if 'ключ' in str(e) or 'лимит' in str(e): break
            progress.progress((n+1)/max(1,len(unresolved)))
        progress.empty()
        if errors: st.warning('\n\n'.join(errors))
        else: st.success('Координаты загружены.')
    st.download_button('Скачать координаты',json.dumps(st.session_state.geo_cache,ensure_ascii=False,indent=2),file_name='coordinates.json',mime='application/json')
    st.caption('Положите coordinates.json рядом с app.py — координаты сохранятся после перезапуска. Сверьте найденные адреса в реестре.')
    complexes=st.multiselect('Жилой комплекс',sorted(objects['ЖК'].unique()))
    responsible=st.multiselect('Ответственный',sorted(objects['Ответственный'].dropna().astype(str).unique()))
    search=st.text_input('Поиск по адресу или названию')
filtered=objects.copy()
if complexes: filtered=filtered[filtered['ЖК'].isin(complexes)]
if responsible: filtered=filtered[filtered['Ответственный'].isin(responsible)]
if search: filtered=filtered[filtered[['Адрес','Объект']].fillna('').agg(' '.join,axis=1).str.contains(search,case=False,regex=False)]
cols=st.columns(4)
for c,label,value in zip(cols,['Домов в выборке','Жилых комплексов','На карте','Без ответственного'],[len(filtered),filtered['ЖК'].nunique(),int(valid(filtered).sum()),int(filtered['Ответственный'].fillna('').eq('').sum())]): c.metric(label,value)
map_tab,registry_tab,route_tab=st.tabs(['Карта и ЖК','Реестр домов','Маршрут А → Б'])
with route_tab:
    st.subheader('Маршрут между двумя адресами')
    with st.form('route'):
        labels=['Другой адрес']+objects['Объект'].tolist()
        left,right=st.columns(2)
        with left: a_select=st.selectbox('Откуда',labels); a_text=st.text_input('Адрес отправления с городом')
        with right: b_select=st.selectbox('Куда',labels,index=min(1,len(labels)-1));b_text=st.text_input('Адрес назначения с городом')
        mode=st.selectbox('Способ передвижения',['Автомобиль','Пешком','Общественный транспорт'])
        submitted=st.form_submit_button('Построить маршрут')
    if submitted:
        st.session_state.pop('route',None)
        try:
            def endpoint(selected,typed):
                if selected=='Другой адрес':
                    if not typed.strip(): raise ValueError('Укажите оба адреса.')
                    return geocode(typed,api_key)
                row=objects[objects['Объект']==selected].iloc[0]
                if valid(objects.loc[[row.name]]).iloc[0]: return {'lat':row['Широта'],'lon':row['Долгота'],'name':row['Адрес']}
                return geocode(row['Адрес'],api_key)
            if not api_key: raise ValueError('Укажите ключ 2ГИС с доступом к маршрутизации.')
            a,b=endpoint(a_select,a_text),endpoint(b_select,b_text)
            with st.spinner('Строим маршрут в 2ГИС…'): route=dgis_route(a['lat'],a['lon'],b['lat'],b['lon'],mode,api_key)
            if not route: raise ValueError('Маршрут не получен. Проверьте доступ к выбранному виду маршрутизации.')
            st.session_state.route={**route,'a':a,'b':b,'mode':mode}
        except ValueError as e: st.error(str(e))
        except Exception: st.error('Не удалось получить маршрут 2ГИС. Проверьте сеть, ключ и доступ к сервису.')
    route=st.session_state.get('route')
    if route:
        st.success(f"{route['mode']}: {route['minutes']:.0f} мин · {route['road_km']:.1f} км")
        st.write(route['a']['name']+' → '+route['b']['name'])
        if not route['segments']: st.warning('2ГИС вернул время и расстояние без линии маршрута.')
        if st.button('Очистить маршрут'): st.session_state.pop('route',None);st.rerun()
with map_tab:
    if filtered.empty: st.info('По этим фильтрам дома не найдены.')
    else:
        missing=int((~valid(filtered)).sum())
        if missing: st.warning(f'У {missing} домов ещё нет координат. Нажмите «Найти недостающие координаты» или заполните широту и долготу в Excel.')
        st_folium(map_view(filtered,api_key,st.session_state.get('route')),height=640,use_container_width=True,returned_objects=[])
        st.caption('На общем плане — названия ЖК и число домов в выборке; при приближении — отдельные дома. Число относится к вашему реестру, а не ко всем корпусам застройщика.')
        st.dataframe(filtered.groupby('ЖК').agg(Домов=('ID','size'),Квартир=('Квартир','sum'),Лифтов=('Лифтов','sum')),use_container_width=True)
with registry_tab:
    st.dataframe(filtered,use_container_width=True,hide_index=True)
    st.download_button('Скачать выборку CSV',filtered.to_csv(index=False,sep=';').encode('utf-8-sig'),file_name='granel_selection.csv',mime='text/csv')
    flagged=filtered[filtered['Проверка данных'].fillna('').ne('')] if 'Проверка данных' in filtered else pd.DataFrame()
    if not flagged.empty:
        st.warning('В исходной таблице есть значения для уточнения. Они сохранены, а не заменены предположениями.')
        st.dataframe(flagged[['Адрес','Проверка данных']],hide_index=True)
    if st.session_state.geo_cache:
        with st.expander('Сверка адресов геокодера'):
            st.dataframe(pd.DataFrame([{'Адрес в Excel':a,'Найдено в 2ГИС':v['name'],'Широта':v['lat'],'Долгота':v['lon']} for a,v in st.session_state.geo_cache.items()]),hide_index=True)
