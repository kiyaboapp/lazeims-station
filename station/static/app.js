// LAZEIMS Station — full production UI
'use strict';

// ── Helpers ───────────────────────────────────────────────────────────────────
const $   = id => document.getElementById(id);
const api   = (url, o) => fetch(url, {credentials:'same-origin',...o});
const jpost = (url, b) => api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const jput  = (url, b) => api(url,{method:'PUT', headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const jdel  = url => api(url,{method:'DELETE'});
const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt = n => Number(n||0).toLocaleString();
const pct = (a,b) => b>0?Math.round(a/b*100):0;

function fullName(s){ return ((s.first_name||'')+(s.middle_name?' '+s.middle_name:'')+' '+(s.surname||'')).trim().toUpperCase(); }
function isValidMark(v){ if(v.trim()==='')return true; const n=Number(v); return Number.isFinite(n)&&n>=0; }
function setMsg(id,txt,isErr){
  const el=$(id); if(!el)return;
  el.textContent=txt;
  el.className='form-msg'+(isErr?' err':txt?' ok':'');
}
function relTime(iso){
  if(!iso) return '—';
  const d=new Date(iso), now=new Date(), diff=Math.floor((now-d)/1000);
  if(diff<60) return 'just now';
  if(diff<3600) return Math.floor(diff/60)+'m ago';
  if(diff<86400) return Math.floor(diff/3600)+'h ago';
  return d.toLocaleDateString();
}

// ── State ─────────────────────────────────────────────────────────────────────
let SESSION=null, CURRENT=null, ROSTER=[], SCOPES=[], SCHOOLS=[];
let ATT={}, ATT_PERSISTED={}, ATT_SAVING={}, MARKS={}, DEBOUNCE_T=null;
let SCOPE_FILTER='all', PORTAL_FILTER='all', POLL_T=null;
let SIDEBAR_OPEN=true;

// ── IndexedDB draft persistence ───────────────────────────────────────────────
const idb=()=>new Promise((res,rej)=>{
  const r=indexedDB.open('lazeims_station',3);
  r.onupgradeneeded=()=>['drafts','marks'].forEach(s=>{if(!r.result.objectStoreNames.contains(s))r.result.createObjectStore(s,{keyPath:'k'});});
  r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);
});
const dbGet=(store,k)=>idb().then(d=>new Promise(res=>{const t=d.transaction(store,'readonly'),rq=t.objectStore(store).get(k);rq.onsuccess=()=>res(rq.result?rq.result.v:null);}));
const dbSet=(store,k,v)=>idb().then(d=>new Promise(res=>{const t=d.transaction(store,'readwrite');t.objectStore(store).put({k,v});t.oncomplete=res;}));
const dbDel=(store,k)=>idb().then(d=>new Promise(res=>{const t=d.transaction(store,'readwrite');t.objectStore(store).delete(k);t.oncomplete=res;}));
const draftKey=()=>`${CURRENT.centre_number}|${CURRENT.subject_code}|${CURRENT.paper_type}`;

// ── View management ───────────────────────────────────────────────────────────
const VIEWS=['login','dashboard','schools','scopes','users','settings','entry-portal','entry'];
function showView(name){
  VIEWS.forEach(v=>{
    const el=$('view-'+v);
    if(el){ el.hidden=(v!==name); el.classList.toggle('active',v===name); }
  });
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
  const noSide=name==='login';
  const sidebar=$('sidebar');
  sidebar.hidden=noSide;
  $('main-area').classList.toggle('no-sidebar',noSide);
  // Close mobile overlay on any navigation
  if(window.innerWidth<769){
    SIDEBAR_OPEN=false;
    sidebar.classList.remove('mobile-open');
    const ov=$('sidebar-overlay'); if(ov) ov.classList.remove('active');
  }
}

// sidebar toggle
function updateSidebarState(){
  const isDesktop=window.innerWidth>=769;
  const sidebar=$('sidebar'), main=$('main-area'), overlay=$('sidebar-overlay');
  if(isDesktop){
    sidebar.classList.toggle('collapsed',!SIDEBAR_OPEN);
    main.classList.toggle('sidebar-collapsed',!SIDEBAR_OPEN);
    sidebar.classList.remove('mobile-open');
    if(overlay) overlay.classList.remove('active');
  } else {
    sidebar.classList.toggle('mobile-open',SIDEBAR_OPEN);
    sidebar.classList.remove('collapsed');
    main.classList.remove('sidebar-collapsed');
    if(overlay) overlay.classList.toggle('active',SIDEBAR_OPEN);
  }
}
$('sidebar-toggle').addEventListener('click',()=>{
  SIDEBAR_OPEN=!SIDEBAR_OPEN;
  updateSidebarState();
});
// close sidebar on mobile overlay tap
const _overlay=$('sidebar-overlay');
if(_overlay) _overlay.addEventListener('click',()=>{ SIDEBAR_OPEN=false; updateSidebarState(); });
window.addEventListener('resize', updateSidebarState);

// nav clicks
document.querySelectorAll('.nav-item').forEach(btn=>btn.addEventListener('click',async()=>{
  const v=btn.dataset.view;
  if(v==='dashboard')    { loadDashboard(); showView('dashboard'); }
  else if(v==='schools') { loadSchools();   showView('schools'); }
  else if(v==='scopes')  { loadScopesView();showView('scopes'); }
  else if(v==='users')   { loadUsers();     showView('users'); }
  else if(v==='settings'){ loadSettings();  showView('settings'); }
  else if(v==='entry-portal'){
    await loadPortal();
    const open=SCOPES.filter(s=>!s.finalized&&s.lock_status!=='LOCKED');
    if(open.length===1){ enterScope(SCOPES.indexOf(open[0])); }
    else { showView('entry-portal'); }
  }
}));

// theme
$('theme-btn').addEventListener('click',()=>{
  const html=document.documentElement;
  const next=html.getAttribute('data-theme')==='dark'?'light':'dark';
  html.setAttribute('data-theme',next);
  try{localStorage.setItem('theme',next);}catch(e){}
});

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot(){
  try{
    const s=await(await api('/api/status')).json();
    const pill=$('status-pill');
    if(s.station_code){
      pill.textContent=`${s.station_code} · ${fmt(s.students)} students · v${s.software_version}`;
      pill.className='pill pill-ok';
    } else {
      pill.textContent='No package — import as admin';
      pill.className='pill pill-warn';
    }
  }catch(e){$('status-pill').textContent='Offline';$('status-pill').className='pill pill-warn';}

  const me=await api('/api/me');
  if(me.ok){ SESSION=await me.json(); afterLogin(); }
  else showView('login');
}

// ── Login ─────────────────────────────────────────────────────────────────────
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const id=$('login-id').value.trim(), secret=$('login-secret').value;
  setMsg('login-msg','Signing in…',false);
  const [dr,ar]=await Promise.allSettled([
    jpost('/api/login/de',{pin:secret,initials:id}),
    jpost('/api/login/admin',{username:id,password:secret}),
  ]);
  const dok=dr.status==='fulfilled'&&dr.value.ok;
  const aok=ar.status==='fulfilled'&&ar.value.ok;
  if(dok){SESSION=await dr.value.json();setMsg('login-msg','',false);afterLogin();}
  else if(aok){SESSION=await ar.value.json();setMsg('login-msg','',false);afterLogin();}
  else setMsg('login-msg','Incorrect credentials.',true);
});

function afterLogin(){
  const isAdmin=SESSION?.role==='EXAM_ADMIN';
  const name=SESSION?.username||SESSION?.initials||'';
  $('who-label').textContent=(isAdmin?'Admin':'DE')+(name?' · '+name:'');
  $('logout-btn').hidden=false;
  document.querySelectorAll('.admin-only').forEach(el=>{ el.hidden=!isAdmin; });
  document.querySelectorAll('.nav-item.admin-only').forEach(el=>{ el.hidden=!isAdmin; });
  // Sidebar profile card
  const prof=$('sidebar-profile');
  if(prof){
    prof.hidden=false;
    const av=$('sp-avatar'); if(av) av.textContent=(name||'?').slice(0,2).toUpperCase();
    const sn=$('sp-name');  if(sn) sn.textContent=name||'—';
    const sr=$('sp-role');  if(sr) sr.textContent=isAdmin?'Admin':'Data Enterer';
  }
  loadDashboard();
  showView('dashboard');
  if(POLL_T) clearInterval(POLL_T);
  POLL_T=setInterval(()=>{ if($('view-dashboard')&&!$('view-dashboard').hidden) loadDashboard(); },30000);
}

$('logout-btn').addEventListener('click',async()=>{
  await jpost('/api/logout',{});
  SESSION=null; CURRENT=null; ROSTER=[];
  $('logout-btn').hidden=true;$('who-label').textContent='';
  if(POLL_T){clearInterval(POLL_T);POLL_T=null;}
  showView('login');
});

// ── DASHBOARD ─────────────────────────────────────────────────────────────────
$('dash-refresh').addEventListener('click', loadDashboard);

async function loadDashboard(){
  let p, schools;
  try{
    [p, schools]=await Promise.all([
      api('/api/progress').then(r=>r.json()),
      api('/api/schools').then(r=>r.json()).catch(()=>[]),
    ]);
    SCHOOLS=schools;
  }catch(e){return;}

  const total=p.total_scopes||0, fin=p.finalized_scopes||0;
  const pctDone=pct(fin,total);
  $('dash-sub').textContent=`${fin}/${total} scopes finalized · ${fmt(p.students)} students`;

  // KPIs
  $('dash-kpis').innerHTML=`
    <div class="kpi ${fin===total&&total>0?'ok':''}">
      <span class="kpi-val">${fin}/${total}</span>
      <span class="kpi-label">Scopes finalized</span>
    </div>
    <div class="kpi">
      <span class="kpi-val">${fmt(p.total_marks)}</span>
      <span class="kpi-label">Marks entered</span>
    </div>
    <div class="kpi">
      <span class="kpi-val">${fmt(p.marks_today)}</span>
      <span class="kpi-label">Entered today</span>
    </div>
    <div class="kpi ${p.pending_events>0?'warn':''}">
      <span class="kpi-val">${fmt(p.pending_events)}</span>
      <span class="kpi-label">Pending sync</span>
    </div>
    <div class="kpi ${p.rejected_events>0?'err':''}">
      <span class="kpi-val">${fmt(p.rejected_events)}</span>
      <span class="kpi-label">Rejected</span>
    </div>
    <div class="kpi">
      <span class="kpi-val">${pctDone}%</span>
      <span class="kpi-label">Complete</span>
    </div>`;

  // Today card
  const todayPct=pct(p.marks_today, p.total_marks||1);
  $('dash-today').innerHTML=`<p>
    <strong>${fmt(p.marks_today)}</strong> marks entered today<br>
    <strong>${fmt(p.total_marks)}</strong> total marks in database<br>
    <strong>${fmt(p.students)}</strong> students on this station
  </p>`;

  // Sync card
  let syncCfg={};
  try{ syncCfg=await(await api('/api/sync/config')).json(); }catch(e){}
  const isAdmin=SESSION?.role==='EXAM_ADMIN';
  $('dash-sync').innerHTML=syncCfg.configured
    ? `<p>&#10003; <strong>Connected</strong><br><span class="muted small">${esc(syncCfg.central_url)}</span><br>${p.pending_events>0?`<span style="color:var(--warn)">${fmt(p.pending_events)} events pending</span>`:'All events synced'}</p>`
    : `<p style="color:var(--warn)">&#9888; Sync not configured${isAdmin?' — go to Sync/Settings to configure':''}</p>`;

  // DE own progress card (for data enterers)
  const isDE=SESSION?.role!=='EXAM_ADMIN';
  const myWrap=$('dash-my-progress-wrap');
  if(isDE&&myWrap){
    myWrap.hidden=false;
    try{
      const det=await api('/api/admin/progress/detail').then(r=>r.ok?r.json():[]).catch(()=>[]);
      const me=det.find(u=>(u.name||'').toUpperCase()===(SESSION?.initials||'').toUpperCase());
      const myMarks=me?me.marks_entered:0;
      const myToday=me?me.marks_today:0;
      const myScopes=me?me.scopes_worked:[]; 
      const myAtt=me?me.attendance_entered:0;
      const myPct=pct(myScopes.filter(s=>s).length, SCOPES.length||1);
      $('dash-my-progress').innerHTML=`<p>
        <strong>${fmt(myToday)}</strong> marks today &nbsp;·&nbsp; <strong>${fmt(myMarks)}</strong> total<br>
        <strong>${fmt(myAtt)}</strong> attendance records<br>
        <strong>${myScopes.length}</strong> scopes worked on
      </p>
      <div class="user-progress-bar-wrap"><div class="user-progress-bar" style="width:${myPct}%"></div></div>`;
    }catch(e){}
  } else if(myWrap){ myWrap.hidden=true; }

  // Schools table — compact, sortable by centre number
  const sorted=[...schools].sort((a,b)=>a.centre_number.localeCompare(b.centre_number));
  $('dash-school-grid').innerHTML=`<table class="dash-schools-tbl">
    <thead><tr><th>Centre</th><th>School Name</th><th>Scopes</th><th>Progress</th></tr></thead>
    <tbody>${sorted.map(s=>{
      const p2=pct(s.finalized_scopes,s.total_scopes);
      const cls=p2===100?'pct-full':p2>0?'pct-part':'pct-empty';
      return `<tr class="dash-school-row" onclick="goSchool('${esc(s.centre_number)}')" style="cursor:pointer">
        <td class="ds-code">${esc(s.centre_number)}</td>
        <td class="ds-name">${esc(s.name||'—')}</td>
        <td class="ds-scopes">${s.finalized_scopes}/${s.total_scopes}</td>
        <td class="ds-pct"><span class="badge ${cls==='pct-full'?'badge-done':cls==='pct-part'?'badge-locked':'badge-open'}">${p2}%</span></td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;
}

window.goSchool=function(code){
  loadSchools().then(()=>{
    showView('schools');
    document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view==='schools'));
    setTimeout(()=>{
      const el=document.querySelector(`[data-school="${code}"]`);
      if(el){el.scrollIntoView({behavior:'smooth',block:'start'});el.click();}
    },200);
  });
};

// ── SCHOOLS ──────────────────────────────────────────────────────────────────
$('school-search').addEventListener('input', renderSchools);

async function loadSchools(){
  try{ SCHOOLS=await(await api('/api/schools')).json(); }catch(e){SCHOOLS=[];}
  renderSchools();
}

function renderSchools(){
  const q=($('school-search').value||'').toLowerCase();
  const list=q?SCHOOLS.filter(s=>s.centre_number.toLowerCase().includes(q)||s.name.toLowerCase().includes(q)):SCHOOLS;
  const fin=SCHOOLS.reduce((a,s)=>a+s.finalized_scopes,0);
  const tot=SCHOOLS.reduce((a,s)=>a+s.total_scopes,0);
  $('schools-sub').textContent=`${SCHOOLS.length} schools · ${fin}/${tot} scopes finalized`;

  if(!list.length){$('schools-list').innerHTML='<p class="no-data">No schools match.</p>';return;}

  $('schools-list').innerHTML=list.map(school=>{
    const p2=pct(school.finalized_scopes, school.total_scopes);
    const full=p2===100&&school.total_scopes>0;
    return `<div class="school-card" data-school="${esc(school.centre_number)}">
      <div class="school-card-header" onclick="toggleSchool(this)">
        <span class="school-h-code">${esc(school.centre_number)}</span>
        <span class="school-h-name">${esc(school.name||'(no name)')}</span>
        <div class="school-h-stats">
          <span class="badge ${full?'badge-done':'badge-open'}">${school.finalized_scopes}/${school.total_scopes}</span>
          <span class="muted small">${fmt(school.students)} students</span>
          <div class="school-h-pbar-wrap"><div class="school-h-pbar ${full?'complete':''}" style="width:${p2}%"></div></div>
          <svg class="school-h-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
        </div>
      </div>
      <div class="school-body">
        ${school.scopes.map(sc=>{
          const mp=pct(sc.marks_entered,sc.students);
          const paper=sc.paper_type.replace('THEORY','T').replace('PRACTICAL','P');
          const isFin=sc.finalized;
          return `<div class="school-scope-row">
            <div class="scope-tag">
              <span class="tag-paper">${esc(paper)}</span>
              <span class="muted small">${esc(sc.subject_name||sc.subject_code)}</span>
              ${isFin?'<span class="badge badge-done" style="font-size:10px">Finalized</span>':sc.lock_status==='LOCKED'?'<span class="badge badge-locked" style="font-size:10px">In use</span>':''}
            </div>
            <div class="scope-nums">
              <span>Marks: <strong>${fmt(sc.marks_entered)}/${fmt(sc.students)}</strong></span>
              <span>Present: <strong>${fmt(sc.att_present)}</strong> Absent: <strong>${fmt(sc.att_absent)}</strong></span>
            </div>
            <div class="scope-bar-wrap"><div class="scope-bar ${isFin?'done':''}" style="width:${mp}%"></div></div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

window.toggleSchool=function(hdr){
  hdr.classList.toggle('open');
  const body=hdr.nextElementSibling;
  body.classList.toggle('open');
  hdr.querySelector('.school-h-chevron').classList.toggle('open');
};

// ── SCOPES VIEW ──────────────────────────────────────────────────────────────
document.querySelectorAll('#scopes-chips .chip').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('#scopes-chips .chip').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); SCOPE_FILTER=b.dataset.f; renderScopesView();
}));

async function loadScopesView(){
  try{ SCOPES=await(await api('/api/scopes')).json(); }catch(e){SCOPES=[];}
  renderScopesView();
}

function renderScopesView(){
  const fin=SCOPES.filter(s=>s.finalized).length;
  $('scopes-sub').textContent=`${fin}/${SCOPES.length} finalized`;
  const list=SCOPES.filter(s=>
    SCOPE_FILTER==='all'||
    (SCOPE_FILTER==='open'&&!s.finalized&&s.lock_status!=='LOCKED')||
    (SCOPE_FILTER==='locked'&&s.lock_status==='LOCKED'&&!s.finalized)||
    (SCOPE_FILTER==='finalized'&&s.finalized)
  );
  if(!list.length){$('scopes-list').innerHTML='<p class="no-data">No scopes match this filter.</p>';return;}
  $('scopes-list').innerHTML=list.map(s=>{
    const locked=!s.finalized&&s.lock_status==='LOCKED';
    const paper=s.paper_type.replace('THEORY','T').replace('PRACTICAL','P');
    const icnCls=s.finalized?'done':locked?'locked':'open';
    const badgeCls=s.finalized?'badge-done':locked?'badge-locked':'badge-open';
    const label=s.finalized?'Finalized':locked?'In use':'Open';
    return `<div class="scope-row ${s.finalized?'finalized':''}">
      <div class="scope-icon ${icnCls}">${esc(paper)}</div>
      <div class="scope-info">
        <div class="scope-centre">${esc(s.centre_number)}${(()=>{const n=(SCHOOLS||[]).find(x=>x.centre_number===s.centre_number);return n&&n.name?` <span class="scope-school-name">${esc(n.name)}</span>`:''})()}</div>
        <div class="scope-subject">${esc(s.subject_code)}${s.subject_name?' · '+esc(s.subject_name):''}</div>
        <div class="scope-paper-tag">${esc(s.paper_type)}</div>
      </div>
      <div class="scope-actions">
        <span class="badge ${badgeCls}">${label}</span>
        ${s.lock_status==='LOCKED'&&SESSION?.role==='EXAM_ADMIN'?`<button class="btn-ghost btn-sm" onclick="forceRelease('${esc(s.centre_number)}','${esc(s.subject_code)}','${esc(s.paper_type)}')">Release lock</button>`:''}
      </div>
    </div>`;
  }).join('');
}

window.forceRelease=async function(cn,sc,pt){
  const reason=prompt('Reason for force-releasing this lock?');
  if(!reason) return;
  const r=await jpost('/api/locks/force-release',{centre_number:cn,subject_code:sc,paper_type:pt,reason});
  if(r.ok){alert('Lock released.');loadScopesView();}
  else{const d=await r.json().catch(()=>({}));alert('Failed: '+(d.detail?.message||'unknown error'));}
};

// ── USERS VIEW ────────────────────────────────────────────────────────────────
let PENDING_SCOPES=[];

function populateScopeDropdowns(){
  const schoolSel=$('new-scope-school');
  const subjSel=$('new-scope-subject');
  if(!schoolSel||!subjSel) return;
  // Schools: centre_number first
  const schools=[...new Set((SCHOOLS||[]).map(s=>s.centre_number))].sort();
  schoolSel.innerHTML='<option value="">All schools</option>'+schools.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
  // Subjects: from scopes
  const subjects=[...new Map((SCOPES||[]).map(s=>[s.subject_code,s.subject_name||s.subject_code]))].sort((a,b)=>a[0].localeCompare(b[0]));
  subjSel.innerHTML='<option value="">All subjects</option>'+subjects.map(([code,name])=>`<option value="${esc(code)}">${esc(code)}${name&&name!==code?' · '+esc(name):''}</option>`).join('');
}

function renderPendingScopes(){
  const el=$('assigned-scopes-list'); if(!el) return;
  if(!PENDING_SCOPES.length){el.innerHTML='<span class="muted small">No restrictions — access to all scopes</span>';return;}
  el.innerHTML=PENDING_SCOPES.map((sc,i)=>`<span class="assigned-scope-chip">${esc(sc.centre_number||'*')} · ${esc(sc.subject_code||'*')} · ${esc(sc.paper_type||'*')}<button type="button" onclick="removePendingScope(${i})">×</button></span>`).join('');
}
window.removePendingScope=function(i){ PENDING_SCOPES.splice(i,1); renderPendingScopes(); };

$('open-create-user').addEventListener('click',()=>{
  $('create-user-panel').hidden=false;
  $('open-create-user').hidden=true;
  PENDING_SCOPES=[];
  populateScopeDropdowns();
  renderPendingScopes();
});
$('cancel-create-user').addEventListener('click',()=>{
  $('create-user-panel').hidden=true;
  $('open-create-user').hidden=false;
  PENDING_SCOPES=[];
  setMsg('create-user-msg','',false);
});

const _addScopeBtn=$('add-scope-btn');
if(_addScopeBtn) _addScopeBtn.addEventListener('click',()=>{
  const cn=($('new-scope-school')?.value||'').trim();
  const sc=($('new-scope-subject')?.value||'').trim();
  const pt=($('new-scope-paper')?.value||'').trim();
  // Require at least one field
  if(!cn&&!sc&&!pt){setMsg('create-user-msg','Pick at least a school, subject or paper to restrict.',true);return;}
  const dup=PENDING_SCOPES.find(s=>s.centre_number===(cn||null)&&s.subject_code===(sc||null)&&s.paper_type===(pt||null));
  if(dup){setMsg('create-user-msg','Scope already added.',true);return;}
  PENDING_SCOPES.push({centre_number:cn||null,subject_code:sc||null,paper_type:pt||null});
  setMsg('create-user-msg','',false);
  renderPendingScopes();
});

async function loadUsers(){
  let users=[], detail=[];
  try{
    [users, detail]=await Promise.all([
      api('/api/admin/users').then(r=>r.ok?r.json():[]),
      api('/api/admin/progress/detail').then(r=>r.ok?r.json():[]),
    ]);
  }catch(e){}

  // Merge detail into users
  const detMap=Object.fromEntries(detail.map(d=>[d.assignment_id,d]));
  const enriched=users.map(u=>({...u,...(detMap[u.assignment_id]||{})}));

  if(!enriched.length){$('users-list').innerHTML='<p class="no-data">No users yet.</p>';return;}

  $('users-list').innerHTML=`<div class="users-list">${enriched.map(u=>{
    const isAdmin=u.role==='EXAM_ADMIN';
    const active=u.active!==false;
    const name=u.initials||u.admin_username||`user_${u.assignment_id}`;
    const fullName=u.full_name||name;
    const roleCls=isAdmin?'role-admin':(active?'role-de':'role-de inactive');
    const roleLabel=isAdmin?'Admin':active?'Data Enterer':'Inactive';
    const worked=(u.scopes_worked||[]);
    const last=relTime(u.last_active);
    return `<div class="user-card">
      <div class="user-card-header">
        <div>
          <div class="user-initials">${esc(name)}</div>
          ${u.full_name?`<div class="user-fullname">${esc(u.full_name)}</div>`:''}
        </div>
        <span class="user-role-badge ${roleCls}">${roleLabel}</span>
      </div>
      ${u.phone?`<div class="user-phone muted small">📞 ${esc(u.phone)}</div>`:''}
      <div class="user-stats">
        <span>Marks: <strong>${fmt(u.marks_entered||0)}</strong></span>
        <span>Today: <strong>${fmt(u.marks_today||0)}</strong></span>
        <span>Attendance: <strong>${fmt(u.attendance_entered||0)}</strong></span>
        <span>Scopes worked: <strong>${worked.length}</strong></span>
      </div>
      <div class="user-stats">
        <span>Last active: <strong>${last}</strong></span>
      </div>
      ${(u.assignments||[]).length>0?`
      <div class="user-scopes-label">Assigned schools</div>
      <div class="user-scope-chips">${(u.assignments||[]).filter(a=>a.centre_number).map(a=>`<span class="user-scope-chip">${esc(a.centre_number)}</span>`).join('')||'<span class="muted small">All</span>'}</div>`:''}
      ${worked.length>0?`
      <div class="user-scopes-label" style="margin-top:8px">Scopes worked on</div>
      <div class="user-scope-chips">${worked.slice(0,6).map(w=>`<span class="user-scope-chip">${esc(w.centre_number)}·${esc(w.subject_code)}·${esc(w.paper_type.replace('THEORY','T').replace('PRACTICAL','P'))}</span>`).join('')}${worked.length>6?`<span class="muted small">+${worked.length-6} more</span>`:''}</div>`:''}
      ${!isAdmin&&active?`<div class="user-card-footer"><button class="btn-danger" onclick="deactivateUser(${u.id},'${esc(name)}')">Remove</button></div>`:''}
    </div>`;
  }).join('')}</div>`;
}

function buildScopeText(assignments){
  if(!assignments.length) return 'All schools';
  const cns=[...new Set(assignments.filter(a=>a.centre_number).map(a=>a.centre_number))];
  return cns.length?cns.slice(0,3).join(', ')+(cns.length>3?` +${cns.length-3}`:''):'All schools';
}

window.deactivateUser=async function(id,name){
  if(!confirm(`Remove account for ${name}? They will no longer be able to sign in.`)) return;
  const r=await jdel(`/api/admin/users/${id}`);
  if(r.ok){setMsg('create-user-msg',`Account for ${name} removed.`,false);loadUsers();}
  else{const d=await r.json().catch(()=>({}));setMsg('create-user-msg',d.detail||'Failed.',true);}
};

$('create-user-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const first_name=($('new-first-name')?.value||'').trim().toUpperCase();
  const middle_name=($('new-middle-name')?.value||'').trim().toUpperCase()||null;
  const surname=($('new-surname')?.value||'').trim().toUpperCase();
  const phone=($('new-phone')?.value||'').trim()||null;
  const initials=$('new-initials').value.trim().toUpperCase();
  const pin=$('new-pin').value.trim();
  if(!first_name){setMsg('create-user-msg','First name required.',true);return;}
  if(!surname){setMsg('create-user-msg','Surname required.',true);return;}
  if(!initials){setMsg('create-user-msg','Initials required.',true);return;}
  if(pin.length<4){setMsg('create-user-msg','PIN must be at least 4 characters.',true);return;}
  // Build scope arrays from PENDING_SCOPES
  const centre_numbers=PENDING_SCOPES.length?[...new Set(PENDING_SCOPES.filter(s=>s.centre_number).map(s=>s.centre_number))]:null;
  const subject_codes=PENDING_SCOPES.length?[...new Set(PENDING_SCOPES.filter(s=>s.subject_code).map(s=>s.subject_code))]:null;
  setMsg('create-user-msg','Creating…',false);
  const r=await jpost('/api/admin/users',{first_name,middle_name,surname,phone,initials,pin,centre_numbers,subject_codes});
  if(r.ok){
    const d=await r.json();
    const displayName=d.full_name||d.initials||initials;
    setMsg('create-user-msg',`Created: ${displayName}`,false);
    $('new-first-name').value='';$('new-middle-name').value='';
    $('new-surname').value='';$('new-phone').value='';
    $('new-initials').value='';$('new-pin').value='';
    PENDING_SCOPES=[];
    $('create-user-panel').hidden=true;$('open-create-user').hidden=false;
    loadUsers();
  } else {
    const d=await r.json().catch(()=>({}));
    setMsg('create-user-msg',d.detail||'Failed.',true);
  }
});

// ── SETTINGS / SYNC ──────────────────────────────────────────────────────────
async function loadSettings(){
  loadSyncConfig();
  // Station info
  try{
    const s=await(await api('/api/status')).json();
    $('station-info-list').innerHTML=[
      ['Station code', s.station_code||'—'],
      ['Exam ID', (s.exam_id||'—').slice(0,16)+'…'],
      ['Students', fmt(s.students)],
      ['Software version', s.software_version||'—'],
      ['Packages imported', s.packages||0],
    ].map(([l,v])=>`<div class="info-row"><span class="info-row-label">${l}</span><span class="info-row-val">${esc(String(v))}</span></div>`).join('');
  }catch(e){}
}

async function loadSyncConfig(){
  try{
    const c=await(await api('/api/sync/config')).json();
    const inp=$('central-url-input');
    if(inp) inp.value=c.central_url||'';
    const banner=$('sync-banner');
    if(banner){
      if(c.configured){
        banner.className='sync-banner ready';
        banner.innerHTML=`&#10003; Ready to sync &nbsp;·&nbsp; <strong>${esc(c.central_url)}</strong>`;
      } else if(!c.central_url){
        banner.className='sync-banner no-url';
        banner.textContent='No Central URL — import a package to configure automatically.';
      } else {
        banner.className='sync-banner no-cred';
        banner.innerHTML=`URL set but no machine credential — re-import the package.`;
      }
    }
  }catch(e){}
  // Show retry button when there are rejected events
  try{
    const s=await(await api('/api/status')).json();
    const btn=$('retry-rejected-btn');
    if(btn){
      if((s.rejected_events||0)>0){
        btn.hidden=false;
        btn.textContent='';
        btn.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.36"/></svg>Retry ${s.rejected_events} rejected`;
      } else {
        btn.hidden=true;
      }
    }
  }catch(e){}
}

$('retry-rejected-btn').addEventListener('click',async()=>{
  setMsg('sync-msg','Resetting rejected events…',false);
  $('sync-result').textContent='';
  const r=await jpost('/api/sync/retry-rejected',{});
  const d=await r.json().catch(()=>({}));
  if(!r.ok){
    setMsg('sync-msg',d.detail||'Failed.',true);
    return;
  }
  setMsg('sync-msg','',false);
  $('sync-result').textContent=`${d.queued} event(s) queued for retry — press Sync now to send.`;
  loadSyncConfig();
});

$('save-url-btn').addEventListener('click',async()=>{
  const url=($('central-url-input').value||'').trim();
  const r=await jpost('/api/sync/config',{central_url:url});
  setMsg('sync-msg',r.ok?'URL saved.':'Admin only.',!r.ok);
  if(r.ok) loadSyncConfig();
});

$('sync-now-btn').addEventListener('click',async()=>{
  setMsg('sync-msg','Syncing…',false);
  $('sync-result').textContent='';
  const r=await jpost('/api/sync/run',{});
  const d=await r.json().catch(()=>({}));
  if(d.configured===false){
    const reason=d.reason||'Central URL not set or no package credential.';
    setMsg('sync-msg','Not configured: '+reason,true);
    return;
  }
  if(d.error){
    setMsg('sync-msg','Network error — Central not reachable: '+d.error,true);
    $('sync-result').textContent='Check that the Central URL is correct and the server is online.';
    return;
  }
  const sent=d.sent??0;
  if(sent===0){
    setMsg('sync-msg','',false);
    $('sync-result').textContent='Nothing to sync — outbox is empty (all events already sent).';
    return;
  }
  const txt=`Sent ${sent}: accepted ${d.accepted??0}, rejected ${d.rejected??0}, duplicates ${d.duplicates??0}`;
  setMsg('sync-msg','',false);
  $('sync-result').textContent=txt;
  if((d.rejected??0)>0) setMsg('sync-msg',`${d.rejected} event(s) rejected by Central — check with admin.`,true);
});

$('import-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const f=$('import-file');
  if(!f.files?.length){setMsg('import-msg','Choose a .zip file.',true);return;}
  const fd=new FormData(); fd.append('file',f.files[0]);
  setMsg('import-msg','Importing…',false);
  const r=await fetch('/api/import',{method:'POST',body:fd,credentials:'same-origin'});
  if(r.ok){
    const d=await r.json().catch(()=>({}));
    setMsg('import-msg','Imported.'+(d.central_url_seeded?' Sync URL set automatically.':'')+' Reloading…',false);
    setTimeout(()=>location.reload(),1400);
  } else {
    const d=await r.json().catch(()=>({}));
    setMsg('import-msg',d.error?.message||'Import failed.',true);
  }
});

// ── ENTRY PORTAL ─────────────────────────────────────────────────────────────
document.querySelectorAll('#portal-chips .chip').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('#portal-chips .chip').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); PORTAL_FILTER=b.dataset.f; renderPortal();
}));

async function loadPortal(){
  try{
    [SCOPES, SCHOOLS]=await Promise.all([
      api('/api/scopes').then(r=>r.json()),
      SCHOOLS.length?Promise.resolve(SCHOOLS):api('/api/schools').then(r=>r.json()).catch(()=>[]),
    ]);
  }catch(e){SCOPES=[];}
  renderPortal();
}

function renderPortal(){
  const list=SCOPES.filter(s=>
    PORTAL_FILTER==='all'||
    (PORTAL_FILTER==='open'&&!s.finalized)||
    (PORTAL_FILTER==='finalized'&&s.finalized)
  );
  if(!list.length){$('portal-scope-list').innerHTML='<p class="no-data">No scopes available.</p>';return;}
  const nameMap=Object.fromEntries((SCHOOLS||[]).map(sc=>[sc.centre_number,sc.name||'']));
  $('portal-scope-list').innerHTML=`<table class="portal-tbl">
    <thead><tr>
      <th>Centre</th><th>School</th><th>Subject</th><th>Paper</th><th>Status</th><th class="pt-action"></th>
    </tr></thead>
    <tbody>${list.map(s=>{
      const idx=SCOPES.indexOf(s);
      const locked=!s.finalized&&s.lock_status==='LOCKED';
      const paper=s.paper_type.replace('THEORY1','T1').replace('THEORY2','T2').replace('PRACTICAL','P');
      const status=s.finalized
        ?'<span class="badge badge-done">Finalized</span>'
        :locked
          ?'<span class="badge badge-locked">In use</span>'
          :'<span class="badge badge-open">Open</span>';
      const btn=(!s.finalized&&!locked)
        ?`<button class="btn-primary btn-sm" onclick="enterScope(${idx})">Enter →</button>`
        :'';
      return `<tr class="${s.finalized?'pt-row-fin':''}${locked?' pt-row-locked':''}">
        <td class="pt-centre">${esc(s.centre_number)}</td>
        <td class="pt-school">${esc(nameMap[s.centre_number]||'')}</td>
        <td class="pt-subject">${esc(s.subject_code)}${s.subject_name?` <span class="pt-subname">${esc(s.subject_name)}</span>`:''}</td>
        <td class="pt-paper">${esc(paper)}</td>
        <td class="pt-status">${status}</td>
        <td class="pt-action">${btn}</td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;
}

// ── DATA ENTRY ────────────────────────────────────────────────────────────────
window.enterScope=async function(i){
  const scope=SCOPES[i];
  const lr=await jpost('/api/locks/acquire',scope);
  if(!lr.ok){const e=await lr.json().catch(()=>({}));alert('Cannot enter scope: '+(e.detail?.message||'locked'));return;}
  CURRENT={...scope};
  $('entry-title').textContent=`${scope.centre_number} · ${scope.subject_code} · ${scope.paper_type}`;
  $('entry-sub').textContent='Loading roster…';
  switchEntryTab('attendance');
  showView('entry');
  await loadRoster();
};

$('entry-back').addEventListener('click',async()=>{
  if(CURRENT) await jpost('/api/locks/release',CURRENT).catch(()=>{});
  CURRENT=null; ROSTER=[];
  await loadPortal();
  showView('entry-portal');
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view==='entry-portal'));
});

// Tabs
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>switchEntryTab(b.dataset.tab)));
function switchEntryTab(tab){
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  $('tab-attendance').hidden=tab!=='attendance';
  $('tab-marks').hidden=tab!=='marks';
  if(tab==='marks') renderMarksTable();
  else renderAttTable();
}

// Roster
async function loadRoster(){
  const q=new URLSearchParams({subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,centre_number:CURRENT.centre_number});
  try{ ROSTER=await(await api('/api/roster?'+q)).json(); }catch(e){ROSTER=[];}
  $('entry-sub').textContent=`${ROSTER.length} students`;
  ATT=Object.fromEntries(ROSTER.map(s=>[s.student_id,s.attendance!==null?s.attendance:true]));
  ATT_PERSISTED=Object.fromEntries(ROSTER.map(s=>[s.student_id,s.attendance!==null]));
  ATT_SAVING={};
  MARKS=Object.fromEntries(ROSTER.map(s=>[s.student_id,{value:'',status:'idle'}]));
  const drafts=await dbGet('marks',draftKey())||{};
  Object.entries(drafts).forEach(([id,v])=>{if(MARKS[id])MARKS[id]={value:v,status:'dirty'};});
  renderAttTable();
  updateEntryBar();
}

// Attendance
function renderAttTable(){
  if(!ROSTER.length){$('att-tbody').innerHTML='<tr><td colspan="4" class="td-empty">No students.</td></tr>';updateAttSummary();return;}
  $('att-tbody').innerHTML=ROSTER.map((s,i)=>{
    const p=ATT[s.student_id]!==false;
    return `<tr class="att-row ${!p?'row-absent':''}" data-i="${i}" tabindex="0" onkeydown="attKey(event,${i},'${esc(s.student_id)}')">
      <td class="col-n">${i+1}</td>
      <td class="col-id">${esc(s.student_id)}</td>
      <td>${esc(fullName(s))}</td>
      <td class="col-att">
        <button class="att-toggle ${p?'att-p':'att-a'}" onclick="attToggle('${esc(s.student_id)}',${i})" aria-label="${p?'Present':'Absent'}">
          <span class="att-lp">P</span><span class="att-knob"></span><span class="att-la">A</span>
        </button>
        ${ATT_SAVING[s.student_id]?'<span class="saving-dot"></span>':''}
      </td>
    </tr>`;
  }).join('');
  updateAttSummary();
}

window.attToggle=async function(sid,idx){
  ATT[sid]=(ATT[sid]===false); ATT_SAVING[sid]=true;
  renderAttTable(); focusAttRow(idx);
  const r=await jput('/api/attendance',{student_id:sid,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,is_present:ATT[sid]!==false,source:'INVIGILATOR_ISAL_TRANSCRIPTION'});
  if(r.ok)ATT_PERSISTED[sid]=true;
  ATT_SAVING[sid]=false; renderAttTable(); focusAttRow(idx); updateEntryBar();
};
window.attKey=function(e,idx,sid){
  if(e.key==='p'||e.key==='P'){e.preventDefault();ATT[sid]=true;attToggle(sid,idx);}
  else if(e.key==='a'||e.key==='A'){e.preventDefault();ATT[sid]=false;attToggle(sid,idx);}
  else if(e.key===' '||e.key==='Enter'){e.preventDefault();attToggle(sid,idx);}
  else if(e.key==='ArrowDown'){e.preventDefault();focusAttRow(Math.min(idx+1,ROSTER.length-1));}
  else if(e.key==='ArrowUp'){e.preventDefault();focusAttRow(Math.max(idx-1,0));}
};
function focusAttRow(i){const rows=$('att-tbody').querySelectorAll('tr');if(rows[i])rows[i].focus();}
function updateAttSummary(){
  const p=ROSTER.filter(s=>ATT[s.student_id]!==false).length;
  const ab=ROSTER.length-p;
  $('att-summary').textContent=`${p} / ${ROSTER.length} present`;
  const hint=$('att-absent-hint');
  if(hint) hint.innerHTML=ab>0?`<span class="absent-warn">${ab} absent</span> — mark before entering marks`:'';
}
$('mark-all-present').addEventListener('click',()=>{
  ROSTER.forEach(s=>{ATT[s.student_id]=true;});
  renderAttTable();
  ROSTER.forEach(s=>jput('/api/attendance',{student_id:s.student_id,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,is_present:true,source:'INVIGILATOR_ISAL_TRANSCRIPTION'}).then(r=>{if(r.ok)ATT_PERSISTED[s.student_id]=true;}));
  updateEntryBar();
});

// Marks
function renderMarksTable(){
  if(!ROSTER.length){$('marks-tbody').innerHTML='<tr><td colspan="6" class="td-empty">No students.</td></tr>';return;}
  const present=ROSTER.filter(s=>ATT[s.student_id]!==false).length;
  const entered=Object.values(MARKS).filter(c=>c.value.trim()!=='').length;
  $('marks-summary').textContent=`${entered} / ${present} marks entered`;
  $('marks-tbody').innerHTML=ROSTER.map((s,i)=>{
    const p=ATT[s.student_id]!==false;
    const cell=MARKS[s.student_id]||{value:'',status:'idle'};
    const inv=!isValidMark(cell.value)&&cell.value!=='';
    return `<tr class="${!p?'row-absent':''}">
      <td class="col-n">${i+1}</td>
      <td class="col-id">${esc(s.student_id)}</td>
      <td>${esc(fullName(s))}</td>
      <td class="col-att"><span class="badge ${p?'badge-open':'badge-locked'}">${p?'P':'A'}</span></td>
      <td class="col-marks">${p?`<div class="marks-cell">
          <input class="marks-inp${inv?' bad':''}" type="text" inputmode="decimal"
            value="${esc(cell.value)}" data-sid="${esc(s.student_id)}" placeholder="marks"
            oninput="marksChange('${esc(s.student_id)}',this.value)"
            onblur="flushMarks()"
            onkeydown="marksKey(event,'${esc(s.student_id)}',${i})"/>
        </div>`:'<span class="muted">—</span>'}</td>
      <td id="mst-${esc(s.student_id)}">${markSt(cell.status,inv)}</td>
    </tr>`;
  }).join('');
  updateEntryBar();
}

function markSt(st,inv){
  if(inv) return '<span class="st-err">Invalid</span>';
  if(st==='saving') return '<span class="st-saving">Saving…</span>';
  if(st==='saved')  return '<span class="st-saved">✓</span>';
  if(st==='dirty')  return '<span class="st-dirty">Unsaved</span>';
  if(st==='error')  return '<span class="st-err">Failed</span>';
  return '';
}

window.marksChange=function(sid,val){
  MARKS[sid]={value:val,status:'dirty'};
  const el=$('mst-'+sid); if(el)el.innerHTML=markSt('dirty',!isValidMark(val)&&val!=='');
  persistDrafts();
  if(DEBOUNCE_T)clearTimeout(DEBOUNCE_T);
  DEBOUNCE_T=setTimeout(flushMarks,800);
  updateMarksSummary();
};
window.marksKey=function(e,sid,idx){
  if(e.key==='Enter'){
    e.preventDefault(); flushMarks();
    // Move to next present student
    for(let j=idx+1;j<ROSTER.length;j++){
      if(ATT[ROSTER[j].student_id]!==false){
        const inp=$('marks-tbody').querySelector(`input[data-sid="${ROSTER[j].student_id}"]`);
        if(inp){inp.focus();inp.select();break;}
      }
    }
  } else if(e.key==='PageDown'){
    e.preventDefault();
    // Next present student
    for(let j=idx+1;j<ROSTER.length;j++){
      if(ATT[ROSTER[j].student_id]!==false){
        const inp=$('marks-tbody').querySelector(`input[data-sid="${ROSTER[j].student_id}"]`);
        if(inp){inp.focus();inp.select();break;}
      }
    }
  } else if(e.key==='PageUp'){
    e.preventDefault();
    // Previous present student
    for(let j=idx-1;j>=0;j--){
      if(ATT[ROSTER[j].student_id]!==false){
        const inp=$('marks-tbody').querySelector(`input[data-sid="${ROSTER[j].student_id}"]`);
        if(inp){inp.focus();inp.select();break;}
      }
    }
  }
};
function updateMarksSummary(){
  const p=ROSTER.filter(s=>ATT[s.student_id]!==false).length;
  const ab=ROSTER.length-p;
  const e=Object.values(MARKS).filter(c=>c.value.trim()!=='').length;
  const el=$('marks-summary');if(el)el.textContent=`${e} / ${p} marks entered`;
  const note=$('marks-absent-note');
  if(note) note.innerHTML=ab>0?`<span class="absent-warn">${ab} absent (skip)</span>`:'';
}
async function persistDrafts(){
  const dirty=Object.fromEntries(Object.entries(MARKS).filter(([,c])=>c.status==='dirty'&&c.value.trim()!=='').map(([id,c])=>[id,c.value]));
  if(Object.keys(dirty).length)await dbSet('marks',draftKey(),dirty);
  else await dbDel('marks',draftKey());
}
async function flushMarks(){
  if(!CURRENT)return;
  const pending=Object.entries(MARKS).filter(([id,c])=>c.status==='dirty'&&c.value.trim()!==''&&isValidMark(c.value)&&ATT[id]!==false);
  if(!pending.length)return;
  updateSaveBadge('saving');
  for(const[sid,cell]of pending){
    MARKS[sid]={...cell,status:'saving'};
    const el=$('mst-'+sid);if(el)el.innerHTML=markSt('saving',false);
    if(!ATT_PERSISTED[sid]){
      const ar=await jput('/api/attendance',{student_id:sid,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,is_present:true,source:'INVIGILATOR_ISAL_TRANSCRIPTION'});
      if(ar.ok)ATT_PERSISTED[sid]=true;
    }
    const r=await jput(`/api/marks/students?student_id=${encodeURIComponent(sid)}`,{subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,mode:'TOTAL_MARKS',total_marks_obtained:Number(cell.value)});
    if(r.ok){MARKS[sid]={value:cell.value,status:'saved'};const idx=ROSTER.findIndex(s=>s.student_id===sid);if(idx>=0)ROSTER[idx].has_marks=true;}
    else{const e=await r.json().catch(()=>({}));MARKS[sid]={value:cell.value,status:'error',error:e.error?.message||'Failed'};}
    const el2=$('mst-'+sid);if(el2)el2.innerHTML=markSt(MARKS[sid].status,false);
  }
  await persistDrafts();
  updateMarksSummary();
  updateEntryBar();
  updateSaveBadge();
}
function updateSaveBadge(force){
  const badge=$('autosave-badge');if(!badge)return;
  const saving=force==='saving'||Object.values(MARKS).some(c=>c.status==='saving');
  const errors=Object.values(MARKS).filter(c=>c.status==='error').length;
  const dirty=Object.values(MARKS).filter(c=>c.status==='dirty').length;
  if(saving){badge.textContent='Saving…';badge.className='save-badge saving';}
  else if(errors){badge.textContent=`${errors} failed`;badge.className='save-badge errors';}
  else if(dirty){badge.textContent=`${dirty} unsaved`;badge.className='save-badge saving';}
  else{badge.textContent='';badge.className='save-badge';}
}
function updateEntryBar(){
  const present=ROSTER.filter(s=>ATT[s.student_id]!==false).length;
  const entered=Object.values(MARKS).filter(c=>c.value.trim()!=='').length;
  const saving=Object.values(MARKS).some(c=>c.status==='saving');
  const errors=Object.values(MARKS).filter(c=>c.status==='error').length;
  const dirty=Object.values(MARKS).filter(c=>c.status==='dirty').length;
  let sv='';
  if(saving)sv='<span class="st-saving">Saving…</span>';
  else if(errors)sv=`<span class="st-err">${errors} failed</span>`;
  else if(dirty)sv=`<span class="st-dirty">${dirty} unsaved</span>`;
  else if(entered)sv='<span class="st-saved">✓ All saved</span>';
  $('entry-statusbar').innerHTML=
    `<span>Present: <strong>${present}</strong>/${ROSTER.length}</span>`+
    `<span>Marks: <strong>${entered}</strong>/${present}</span>`+
    (sv?`<span>${sv}</span>`:'');
}

$('save-attendance').addEventListener('click',async()=>{
  if(!CURRENT)return;
  const btn=$('save-attendance');
  btn.disabled=true;btn.textContent='Saving…';
  const todo=ROSTER.filter(s=>!ATT_PERSISTED[s.student_id]);
  let failed=[];
  for(const s of todo){
    ATT_SAVING[s.student_id]=true;renderAttTable();
    const r=await jput('/api/attendance',{student_id:s.student_id,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,is_present:ATT[s.student_id]!==false,source:'INVIGILATOR_ISAL_TRANSCRIPTION'});
    if(r.ok)ATT_PERSISTED[s.student_id]=true; else failed.push(s.student_id);
    ATT_SAVING[s.student_id]=false;renderAttTable();
  }
  const box=$('att-validation');box.hidden=false;
  if(!failed.length){
    box.className='marks-validation ok';
    box.innerHTML=`<div class="mv-title">✓ Attendance saved for all ${ROSTER.length} student(s).</div>`;
    btn.classList.add('done');
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="15" height="15"><polyline points="20 6 9 17 4 12"/></svg> All saved — opening Marks…';
    btn.disabled=true;
    setTimeout(()=>switchEntryTab('marks'), 800);
  } else {
    box.className='marks-validation blocked';
    box.innerHTML=`<div class="mv-title">${failed.length} student(s) failed to save:</div><div class="mv-list">${failed.map(id=>`<span class="missing-chip">${esc(id)}</span>`).join('')}</div>`;
    btn.disabled=false;
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="15" height="15"><polyline points="20 6 9 17 4 12"/></svg> Save &amp; continue to Marks';
  }
});

// Finalize
$('finalize-btn').addEventListener('click',async()=>{
  if(!confirm('Finalize this scope? This cannot be undone.'))return;
  await flushMarks();
  const r=await jpost('/api/scopes/finalize',CURRENT);
  if(r.ok){
    setMsg('entry-msg','Scope finalized.',false);
    await loadPortal();
    setTimeout(()=>$('entry-back').click(),1400);
  } else {
    const d=await r.json().catch(()=>({}));
    const b=(d.detail?.result?.blockers||[]).map(x=>x.message).join('; ');
    setMsg('entry-msg','Cannot finalize: '+(b||'incomplete data'),true);
  }
});

// Completeness check (bottom of Marks tab)
function jumpToStudent(sid){
  switchEntryTab('marks');
  const inp=$('marks-tbody').querySelector(`input[data-sid="${sid}"]`);
  if(inp){
    inp.scrollIntoView({block:'center',behavior:'smooth'});
    inp.focus();inp.select();
    inp.classList.add('jump-highlight');
    setTimeout(()=>inp.classList.remove('jump-highlight'),1500);
  }
}
window.jumpToStudent=jumpToStudent;

$('check-completeness').addEventListener('click',async()=>{
  if(!CURRENT)return;
  const btn=$('check-completeness');
  btn.disabled=true;btn.textContent='Checking…';
  await flushMarks();
  const box=$('marks-validation');
  try{
    const q=new URLSearchParams({centre_number:CURRENT.centre_number,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type});
    const res=await(await api('/api/scopes/validation?'+q)).json();
    box.hidden=false;
    if(res.complete){
      box.className='marks-validation ok';
      box.innerHTML=`<div class="mv-title">✓ Complete — every present student has marks.</div>You can finalize this scope now.`;
    } else {
      const missing=(res.blockers||[]).filter(b=>b.student_id&&b.code==='BLANK_MARK_NOT_ALLOWED');
      const scopeWide=(res.blockers||[]).filter(b=>!b.student_id);
      const other=(res.blockers||[]).filter(b=>b.student_id&&b.code!=='BLANK_MARK_NOT_ALLOWED');
      box.className='marks-validation blocked';
      box.innerHTML=`<div class="mv-title">${missing.length+other.length} present student(s) still need attention before finalizing:</div>`+
        `<div class="mv-list">${[...missing,...other].map(b=>`<button type="button" class="missing-chip" onclick="jumpToStudent('${esc(b.student_id)}')" title="${esc(b.message)}">${esc(b.student_id)}</button>`).join('')}</div>`+
        (scopeWide.length?`<div style="margin-top:8px">${scopeWide.map(b=>esc(b.message)).join('<br>')}</div>`:'')+
        `<div style="margin-top:8px" class="muted small">Click a chip to jump to that student. Blank marks can never be saved or finalized directly — that's a hard rule. If a mark is genuinely missing (e.g. a missing script), use Force complete below to raise a tracked incident explaining why instead of leaving it silently blank.</div>`+
        (missing.length?`<div style="margin-top:10px"><button type="button" id="force-complete-btn" class="btn-warning btn-sm">Force complete (raise incident for ${missing.length} student${missing.length>1?'s':''})</button></div>`:'');
      if(missing.length){
        $('force-complete-btn').addEventListener('click',async()=>{
          const reason=prompt(`Explain why these ${missing.length} present student(s) have no mark (e.g. "Missing script, awaiting invigilator"). This raises an OPEN incident per student — it does NOT fill in a mark, and the scope still can't finalize until each incident is resolved.`);
          if(!reason)return;
          const fbtn=$('force-complete-btn');fbtn.disabled=true;fbtn.textContent='Raising incidents…';
          for(const b of missing){
            await jpost('/api/incidents',{student_id:b.student_id,subject_code:CURRENT.subject_code,paper_type:CURRENT.paper_type,incident_type:'OTHER',explanation:reason});
          }
          $('check-completeness').click();
        });
      }
    }
  } catch(e){
    box.hidden=false;box.className='marks-validation blocked';
    box.innerHTML='Could not check completeness. Try again.';
  }
  btn.disabled=false;btn.textContent='Check completeness';
});

// ── START ─────────────────────────────────────────────────────────────────────
boot();