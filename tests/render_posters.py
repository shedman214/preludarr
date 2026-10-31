# SPDX-License-Identifier: GPL-3.0-or-later
import os,sys,tempfile,base64
w=tempfile.mkdtemp(); os.makedirs(w+'/a'); os.makedirs(w+'/m')
os.environ.update(CONFIG_DIR=w+'/c',TV_ROOT=w+'/a',MOVIE_ROOT=w+'/m',AUTO_DOWNLOAD='0',POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0,'/src/preludarr'); import app, poster_manual as pm
def art(col): return 'data:image/svg+xml;base64,'+base64.b64encode(f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="{col}"/></svg>'.encode()).decode()
c=[{'provider':'TVDB','title':'Bubbles','year':'2022','poster':art('#5a6f9a')},
   {'provider':'TMDB','title':'Bubble','year':'2022','poster':art('#3b8fb0')},
   {'provider':'TMDB','title':'Bubble','year':'2021','poster':art('#8a6fb0')},
   {'provider':'TVDB','title':'Bubble','year':'2022','poster':art('#2f9e7a')},
   {'provider':'TMDB','title':'Something Else','year':'2010','poster':art('#a05a5a')},
   {'provider':'TMDB','title':'バブル','original':'Bubble','year':'2022','poster':art('#b08a3b')}]
h=pm._grid('/movies/Bubble (2022)',c,'search','tok',True,app,'Bubble (2022)')
shell='<!doctype html><html><head><meta charset="utf-8"><style>body{background:#0e1422;color:#e7ecf6;font:15px system-ui;margin:0;padding:24px;max-width:1100px}button{background:#4f8ff7;color:#fff;border:0;border-radius:8px;padding:8px 12px;font-weight:600}.muted{color:#9fb0cc}</style></head><body>'
open('/out/posters.html','w').write(shell+pm.PANEL_CSS+'<section class="panel" id="themearr-poster-panel"><h4>Search results for “Bubble”</h4>'+h+'</section></body></html>')
print('ok')
