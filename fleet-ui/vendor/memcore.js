/* ================= Memory Core =================
   The LLM wiki as a zoomable map, and how Computer searches it.
   Map: every page is a dot (colour = page type, size = inbound link weight), every wiki link a line (colour = relation).
     Network layout: a force simulation pulls linked pages together, so clusters are topics. It settles once, is kept in
     this browser (localStorage "mcPos", keyed by a hash of the page path) and only new pages move later.
     Core layout: one ring per page type around the core (the original Memory Core picture).
     Zoom with wheel, pinch or the buttons, drag to move; names appear as you zoom in.
   Connections: click a page = its neighbourhood, double-click = zoom to it, Shift-click a second page = shortest path.
     Probe: type terms; each term becomes a pill wired to the pages whose title, tags or summary contain it; pages that
     several terms share are the bridges between the terms.
   Computer: a session is drawn as search anatomy: term pills -> entry pages (catalogue match) -> pages reached over a
     relation (dashed) -> full-text fallback (dotted); related fans, numbered reads, cited pages, and the course between
     them. The bar under the map replays it step by step; the camera follows.
   Data: /api/computer/wiki/graph (memory-core's own link graph) and /api/computer/wiki/trails (Computer's chat files), both
   behind the Starfleet key like the rest of Computer. Read-only. Uses fleet.html's compJSON, esc, ago, view. */
(()=>{
const TYPES={other:["Other","#f96",.22],concept:["Concepts","#9cf",.45],entity:["Entities","#fc6",.67],source:["Sources","#c9c",.89]};
const ORDER=["other","concept","entity","source"];
const ONE={other:"Other",concept:"Concept",entity:"Entity",source:"Source"};
const EDGE={r:["related","#c9c"],l:["link","#6f93bd"],s:["source","#a4855f"]};
const TERMC=["#ff9900","#99ccff","#ffcc66","#cc99cc","#ff9966","#d46ab8"];
const KIND={ask:"Query",overview:"Scan",search:"Search",related:"Related",read:"Read",answer:"Answer",web:"Web"};
const C={course:"#f90",hit:"#f90",read:"#fff",readRing:"#9cf",cite:"#d46ab8",rel:"#c9c",dim:"#3a2c18",path:"#fff"};
const CORE_R=600, KMAX=9, LINK=28, CHARGE=-90, GRAV=.03, DMAX2=300*300, LSTR=1.7, POS="mcPos2", TAU=Math.PI*2;
const STOP=new Set(("the a an and or of in on at for to is are was be what which how why do does did my me i you with from about into "+
  "der die das und oder ein eine einen für mit von zu ist sind was wie warum im in auf den dem").split(" "));
const $=id=>document.getElementById(id);
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const tplus=s=>{ s=Math.max(0,Math.round(s)); return "T+"+(s<60?s+"s":Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s"); };
const hash=s=>{ let h=2166136261; for(let i=0;i<s.length;i++){ h^=s.charCodeAt(i); h=Math.imul(h,16777619); } return (h>>>0).toString(36); };
const LS={get(k,d){ try{ const v=localStorage.getItem(k); return v==null?d:JSON.parse(v); }catch(e){ return d; } },
          set(k,v){ try{ localStorage.setItem(k,JSON.stringify(v)); }catch(e){} }};
const termsOf=q=>[...new Set(String(q||"").toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(t=>t.length>1&&!STOP.has(t)))].slice(0,6);
const has=(n,t)=>n.lt.includes(t)||n.ls.includes(t);

let G=null, gSig="", gAt=0, byPath={}, byName={}, trails=[], chatSel=LS.get("mcNoChat",false)?"":null,   /* "": no session on the map, chosen by the user */
    stepHot=null, stepMax=Infinity, sel=null, hov=null;
let mode=LS.get("mcMode","net")==="core"?"core":"net", coreAmt=mode==="core"?1:0, showLinks=LS.get("mcLinks",true)!==false, showHeat=false;
let heat={}, sectors=[], focus=null, probe=[], pathSel=null, pathMode=false, play=null, speed=1, follow=true, stepAt={};
let cam={x:0,y:0,k:.5}, camTo=null, userCam=false, kfit=.5, W=0, H=0, dpr=1, O={x:0,y:0}, sim=null;
let placed=[], dirty=true, raf=0, timer=null, built=false, bctx=null, octx=null, lastAct=0, tipXY=null;

/* ---------------------------------------------------------------- page */
function build(){
  $("wikiview").innerHTML=`
    <div class="top"><div class="title">Memory <span>Core</span></div><div class="bar"><span id="mcmeta">--</span></div></div>
    <div class="vmsg" id="mcmsg"></div>
    <div id="mc">
      <div id="mcl"><div class="mch">Computer sessions<button type="button" id="mcnosel" title="Take the session off the map: wiki pages only">Clear</button></div><div id="mcchats"></div>
        <div class="mch">Search terms</div><div id="mcterms"></div>
        <div class="mch">Most consulted</div><div id="mctop"></div></div>
      <div id="mcc">
        <div id="mctools"><input id="mcq" placeholder="Probe terms or page" autocomplete="off" list="mcql"
            title="Type search terms to see which pages they touch and how they connect. Pick a page name to jump to it."><datalist id="mcql"></datalist>
          <button class="pill" id="mcnet" type="button" title="Linked pages pulled together: clusters are topics">Network</button>
          <button class="pill" id="mccore" type="button" title="One ring per page type around the core">Core</button>
          <span class="sep"></span>
          <button class="pill" id="mclinks" type="button" title="Draw every link of the wiki">Links</button>
          <button class="pill" id="mcheat" type="button" title="Colour pages by how often Computer consulted them, over all sessions">Heat</button>
          <span class="sep"></span>
          <button class="pill" id="mcrawb" type="button" title="Sources waiting in the Knowledge DB's .raw folder: see them and ingest the new ones">Raw</button>
          <span class="grow"></span><span id="mcnos" title="Ask Computer about your notes. Its search then shows here: sessions, search terms, an access log and a replay bar.">No Computer session yet</span><span id="mcnote"></span></div>
        <div id="mcbox"><canvas id="mccv"></canvas><canvas id="mcov"></canvas><div id="mccap"></div><div id="mctip"></div>
          <div id="mclegend"></div>
          <div id="mchint">Scroll or pinch: zoom · Drag: move · Click a page: its neighbourhood · Double-click: zoom to it · Shift-click a second page: shortest path</div>
          <div id="mczoom"><button class="zb" id="mczh" type="button" title="How to move around the map">?</button><button class="zb" id="mczo" type="button" title="Zoom out (-)">&minus;</button><button class="zb" id="mczf" type="button"
            title="Fit the whole wiki (0)">Fit</button><button class="zb" id="mczi" type="button" title="Zoom in (+)">+</button><span id="mczl">100%</span></div></div>
        <div id="mctl"></div></div>
      <div id="mcr"><div id="mcprobe"></div><div id="mcpage"></div><div class="mch" id="mclogh">Access log</div><div id="mclog"></div></div>
    </div>`;
  $("mczh").onclick=()=>$("mchint").classList.toggle("on");
  bctx=$("mccv").getContext("2d"); octx=$("mcov").getContext("2d");
  const pills=()=>{ $("mcnet").classList.toggle("on",mode==="net"); $("mccore").classList.toggle("on",mode==="core");
    $("mclinks").classList.toggle("on",showLinks); $("mcheat").classList.toggle("on",showHeat); };
  const setMode=m=>{ mode=m; LS.set("mcMode",m); userCam=false; if(G) camTo=fitAll(); pills(); touch(); };
  $("mcnet").onclick=()=>setMode("net"); $("mccore").onclick=()=>setMode("core");
  $("mclinks").title="Related links always show; plain links appear as you zoom in";
  $("mclinks").onclick=()=>{ showLinks=!showLinks; LS.set("mcLinks",showLinks); pills(); touch(); };
  $("mcnosel").onclick=()=>pickChat("");
  $("mcrawb").onclick=()=>$("mcraw")?closeRaw():openRaw();
  $("mcheat").onclick=()=>{ showHeat=!showHeat; pills(); touch(); };
  pills();
  $("mczi").onclick=()=>zoomBy(1.5); $("mczo").onclick=()=>zoomBy(1/1.5);
  $("mczf").onclick=()=>{ userCam=false; if(G) camTo=fitAll(); kick(); };
  let qt=0;
  $("mcq").addEventListener("input",()=>{ clearTimeout(qt); qt=setTimeout(()=>setProbe($("mcq").value),220); });
  $("mcq").addEventListener("change",()=>{ const v=$("mcq").value.trim().toLowerCase(), n=v&&byName[v];
    if(n&&n.t.toLowerCase()===v){ clearTimeout(qt); $("mcq").value=""; setProbe(""); select(n,true); } });
  pointer($("mcov"));
  addEventListener("resize",()=>{ if(view==="wiki"&&resize()){ if(!userCam&&G) camTo=fitAll(); touch(); } });
  addEventListener("keydown",e=>{
    if(view!=="wiki"||e.ctrlKey||e.metaKey||e.altKey||/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if(e.key==="+"||e.key==="=") zoomBy(1.5); else if(e.key==="-") zoomBy(1/1.5); else if(e.key==="0") $("mczf").click(); });
  document.addEventListener("visibilitychange",kick);
  $("wikiview").addEventListener("pointermove",()=>{ const idle=performance.now()-lastAct>20000; lastAct=performance.now(); if(idle) kick(); });
  built=true;
}
function msg(t,err){   /* an error is a bar across the view; a note (wiki service stopped, map still shown) is a small chip */
  const m=$("mcmsg"), c=$("mcnote"); m.textContent=err?t:""; m.style.display=t&&err?"block":"none"; m.classList.toggle("err",!!(t&&err));
  c.textContent=t&&!err?"Wiki service stopped":""; c.title=t&&!err?t:""; c.style.display=t&&!err?"inline-block":"none";
}
function shape(){
  if(!built) return;
  const none=!trails.length, m=$("mc");
  m.classList.toggle("nol",none); m.classList.toggle("nor",none&&!sel&&!probe.length);
  m.classList.toggle("nos",!none&&!trailOf()); $("mcnosel").hidden=!trailOf();   /* nos: sessions exist, none shown */
  $("mcnos").style.display=none?"inline-block":"none";
  touch();
}
function legend(){
  const cnt=k=>G?G.nodes.filter(n=>n.ty===k).length:"";
  $("mclegend").innerHTML=["concept","entity","source","other"].map(k=>`<span><i style="background:${TYPES[k][1]}"></i>${TYPES[k][0]} <b>${cnt(k)}</b></span>`).join("")+
    ["r","l"].map(k=>`<span><i class="ln" style="background:${EDGE[k][1]}"></i>${EDGE[k][0]}</span>`).join("");   /* one line: page types and the two common relations */
}
function resize(){
  const box=$("mcbox"), w=box.clientWidth, h=box.clientHeight, d=devicePixelRatio||1;
  if(!w||!h||(w===W&&h===H&&d===dpr)) return false;
  W=w; H=h; dpr=d;
  for(const c of [$("mccv"),$("mcov")]){ c.width=Math.round(W*dpr); c.height=Math.round(H*dpr); }
  return true;
}
function touch(){ dirty=true; kick(); }
function kick(){ if(!raf) raf=requestAnimationFrame(frame); }

/* ---------------------------------------------------------------- data */
async function loadGraph(){
  try{
    const j=await compJSON("/api/computer/wiki/graph");
    if(j.error) msg(j.error,true); else msg(j.note||"");   /* note: memory-core is stopped (the map still reads the vault) */
    if(!j.graph){ setTimeout(()=>{ if(view==="wiki") loadGraph(); },3000); return; }
    if(G&&j.at===gAt) return;
    gAt=j.at;
    const sig=j.graph.nodes.length+"/"+j.graph.edges.length+"/"+hash(j.graph.nodes.map(n=>n[0]+n[4]).join("|"));
    if(sig!==gSig){ gSig=sig; setGraph(j.graph); prep(); paintLists(); refocus(); }
  }catch(e){ msg(e.message,true); }
}
async function loadTrails(){
  try{
    const j=await compJSON("/api/computer/wiki/trails");
    const sig=a=>JSON.stringify(a.map(t=>[t.id,t.steps.length])), next=j.trails||[];
    if(sig(next)===sig(trails)) return;      /* unchanged: keep the prepared copy */
    trails=next;
    if(chatSel!==""&&!trails.find(t=>t.id===chatSel)) chatSel=trails[0]?trails[0].id:null;   /* newest session by default: a live answer grows */
    prep(); paintLists(); refocus();
  }catch(e){ msg(e.message,true); }
}
function setGraph(g){
  const old=G?Object.fromEntries(G.nodes.map(n=>[n.p,n])):{}, saved=LS.get(POS,{}), keepSel=sel&&sel.p;
  byPath={}; byName={};
  const nodes=g.nodes.map(([p,t,type,tags,w,sum],i)=>({i,p,t:t||p,type,tags:tags||[],w:w||0,sum:sum||"",ty:TYPES[type]?type:"other",
    adj:[],r:2.6+1.5*Math.sqrt(w||0),h:hash(p)}));
  nodes.forEach(n=>{ const stem=n.p.split("/").pop().replace(/\.md$/,"");
    n.lt=(n.t+" "+n.tags.join(" ")+" "+stem).toLowerCase(); n.ls=n.sum.toLowerCase();
    byPath[n.p]=n; byName[n.t.toLowerCase()]=byName[n.t.toLowerCase()]||n; byName[stem.toLowerCase()]=byName[stem.toLowerCase()]||n; });
  const ek={l:[],r:[],s:[]};
  g.edges.forEach(([u,v,k])=>{ nodes[u].adj.push([nodes[v],k,1]); nodes[v].adj.push([nodes[u],k,0]); (ek[k]||ek.l).push([nodes[u],nodes[v]]); });
  G={nodes,edges:g.edges,ek,byW:[...nodes].sort((a,b)=>b.w-a.w),stubs:g.stubs,log:g.log_mtime};
  coreLayout(); sectorLabels();
  let fresh=0;
  nodes.forEach(n=>{ const o=old[n.p], q=o?[o.nx,o.ny]:saved[n.h];
    if(q){ n.nx=q[0]; n.ny=q[1]; n.ok=true; } else fresh++;
    n.x=o?o.x:n.cx*.5; n.y=o?o.y:n.cy*.5; });
  nodes.forEach(n=>{ if(n.ok) return; const m=fresh<nodes.length&&n.adj.find(a=>a[0].ok);   /* a new page starts beside a placed neighbour */
    if(m){ n.nx=m[0].nx+6+n.i%9; n.ny=m[0].ny+6+n.i%7; } else { n.nx=n.cx*.5; n.ny=n.cy*.5; } });
  if(fresh) startSim(fresh===nodes.length?1:.25);
  sel=keepSel?byPath[keepSel]||null:null; hov=null; pathSel=null;
  resize();
  if(!userCam){ cam=fit(nodes,24,null,true); camTo=fitAll(); }
  $("mcql").innerHTML=nodes.map(n=>`<option value="${esc(n.t)}">`).join("");
  $("mcmeta").textContent=`${nodes.length} pages · ${g.edges.length} links`+(g.log_mtime?` · last change ${ago(g.log_mtime)}`:"");
  legend();
  if(probe.length) setProbe($("mcq").value);
  paintCard();
}
function resolve(s){
  if(!s||!G) return null;
  s=String(s).trim();
  return byPath[s]||byPath["wiki/"+s]||byName[s.toLowerCase()]||byName[s.split("/").pop().replace(/\.md$/i,"").toLowerCase()]||null;
}
/* resolve every step of every session against the graph once */
function prep(){
  heat={};
  const bump=(n,v)=>{ if(n) heat[n.i]=(heat[n.i]||0)+v; };
  trails.forEach(t=>t.steps.forEach(s=>{
    s._n=resolve(s.target)||null;
    s._hits=(s.hits||[]).map(h=>({n:resolve(h.p),via:h.via?resolve(h.via):null,text:!!h.text,rel:h.rel})).filter(h=>h.n);
    s._cites=[...new Set((s.cites||[]).map(resolve).filter(Boolean))];
    s._terms=s.tool==="search"?termsOf(s.arg):[];
    s._nodes=[...new Set([s._n,...s._hits.map(h=>h.n),...s._hits.map(h=>h.via),...s._cites].filter(Boolean))];
    const e=s._hits.find(h=>!h.via&&!h.text)||s._hits[0];
    s._focus=s.tool==="read"||s.tool==="related"?s._n:s.tool==="search"?(e?e.n:null):null;
    if(s.tool==="read") bump(s._n,3);
    s._hits.forEach(h=>bump(h.n,1)); s._cites.forEach(n=>bump(n,2));
  }));
}
function trailOf(){ return trails.find(t=>t.id===chatSel)||null; }
function shown(){ const t=trailOf(), a=[]; if(t) t.steps.forEach((s,k)=>{ if(stepHot!=null?k===stepHot:k<stepMax) a.push([s,k]); }); return a; }
function courseOf(steps){ const pts=[]; steps.forEach(([s,k])=>{ const n=s._focus; if(n&&(!pts.length||pts[pts.length-1].n!==n)) pts.push({n,k,s}); }); return pts; }

/* ---------------------------------------------------------------- layouts */
/* Core: rings by type, linked pages pulled together (circular barycentre, rings kept evenly filled). */
function coreLayout(){
  const N=G.nodes, rings=ORDER.map(k=>N.filter(n=>n.ty===k));
  rings.forEach(r=>{ r.sort((a,b)=>((a.tags[1]||"~")+a.t).localeCompare((b.tags[1]||"~")+b.t)); r.forEach((n,k)=>n.a=TAU*k/r.length); });
  for(let it=0;it<24;it++){
    N.forEach(n=>{ let s=0,c=0; for(const [m,k] of n.adj){ const w=k==="r"?1:.6; s+=w*Math.sin(m.a); c+=w*Math.cos(m.a); }
      const t=(s||c)?Math.atan2(s,c):n.a, d=Math.atan2(Math.sin(t-n.a),Math.cos(t-n.a)); n.t2=(n.a+.7*d+TAU)%TAU; });
    rings.forEach(r=>{ if(!r.length) return; r.sort((a,b)=>a.t2-b.t2);
      let s=0,c=0; r.forEach((n,k)=>{ const d=n.t2-TAU*k/r.length; s+=Math.sin(d); c+=Math.cos(d); });
      const ph=Math.atan2(s,c); r.forEach((n,k)=>n.a=(TAU*k/r.length+ph+TAU)%TAU); });
  }
  N.forEach(n=>{ const r=CORE_R*TYPES[n.ty][2]; n.cx=r*Math.cos(n.a-Math.PI/2); n.cy=r*Math.sin(n.a-Math.PI/2); });
}
function sectorLabels(){
  const S=18, cnt=Array.from({length:S},()=>({})), all={};
  G.nodes.forEach(n=>n.tags.forEach(t=>all[t]=(all[t]||0)+1));
  const skip=new Set(["concept","entity","source","meta","session","comparison","reference","question"]);
  G.nodes.forEach(n=>{ const s=Math.floor(n.a/TAU*S)%S;
    n.tags.forEach(t=>{ if(!skip.has(t)&&all[t]<G.nodes.length*.12) cnt[s][t]=(cnt[s][t]||0)+1; }); });
  sectors=cnt.map((c,s)=>{ const [t,v]=Object.entries(c).sort((a,b)=>b[1]-a[1])[0]||[]; return v>=4?{a:(s+.5)/S*TAU,t}:null; });
  sectors=sectors.map((x,s)=>x&&sectors[(s+S-1)%S]&&sectors[(s+S-1)%S].t===x.t?null:x);
}
/* Network: springs along links (weaker at hubs), every page repels every other, light gravity. About 300 rounds,
   a few per frame, so the map settles while you watch. ponytail: O(n^2) repulsion, fine to a few thousand pages;
   a quadtree (Barnes-Hut) is the upgrade when the wiki outgrows that. */
function startSim(alpha){
  const N=G.nodes, n=N.length, E=G.edges, m=E.length, deg=new Float64Array(n);
  const s={alpha,n,m,X:new Float64Array(n),Y:new Float64Array(n),VX:new Float64Array(n),VY:new Float64Array(n),
    eu:new Int32Array(m),ev:new Int32Array(m),es:new Float64Array(m),eb:new Float64Array(m),R:new Float64Array(n)};
  N.forEach((p,i)=>{ s.X[i]=p.nx; s.Y[i]=p.ny; s.R[i]=p.r*1.5+1.5; });      /* the room a dot needs at overview zoom */
  E.forEach(([u,v])=>{ deg[u]++; deg[v]++; });
  E.forEach(([u,v],i)=>{ s.eu[i]=u; s.ev[i]=v; s.es[i]=Math.min(1,LSTR/Math.min(deg[u],deg[v])); s.eb[i]=deg[u]/(deg[u]+deg[v]); });
  sim=s;
}
function simTick(){
  const s=sim,{X,Y,VX,VY,n,m,eu,ev,es,eb,R}=s, a=s.alpha;
  for(let i=0;i<m;i++){ const u=eu[i], v=ev[i]; let dx=X[v]+VX[v]-X[u]-VX[u]||1e-6, dy=Y[v]+VY[v]-Y[u]-VY[u]||1e-6;
    const d=Math.sqrt(dx*dx+dy*dy), l=(d-LINK)/d*a*es[i]; dx*=l; dy*=l;
    VX[v]-=dx*eb[i]; VY[v]-=dy*eb[i]; VX[u]+=dx*(1-eb[i]); VY[u]+=dy*(1-eb[i]); }
  for(let i=0;i<n;i++){ const xi=X[i], yi=Y[i]; let fx=0, fy=0;
    for(let j=i+1;j<n;j++){ let dx=X[j]-xi, dy=Y[j]-yi, d2=dx*dx+dy*dy;
      if(d2>DMAX2) continue;
      if(d2===0){ dx=.3+(j%5)*.1; dy=.3+(i%5)*.1; d2=dx*dx+dy*dy; }
      const rr=R[i]+R[j];
      if(d2<rr*rr){ const d=Math.sqrt(d2), q=(rr-d)/d*.3; fx-=dx*q; fy-=dy*q; VX[j]+=dx*q; VY[j]+=dy*q; }   /* dots never sit on each other */
      if(d2<16) d2=16;
      const f=CHARGE*a/d2; fx+=dx*f; fy+=dy*f; VX[j]-=dx*f; VY[j]-=dy*f; }
    VX[i]+=fx; VY[i]+=fy; }
  const N=G.nodes;
  for(let i=0;i<n;i++){ VX[i]=(VX[i]-X[i]*GRAV*a)*.6; VY[i]=(VY[i]-Y[i]*GRAV*a)*.6; X[i]+=VX[i]; Y[i]+=VY[i]; N[i].nx=X[i]; N[i].ny=Y[i]; }
  s.alpha-=s.alpha*.0228;
  if(s.alpha<.004){ sim=null; const o={}; N.forEach(p=>o[p.h]=[Math.round(p.nx*10)/10,Math.round(p.ny*10)/10]); LS.set(POS,o); try{ localStorage.removeItem("mcPos"); }catch(e){} }
}

/* ---------------------------------------------------------------- camera */
function fit(ns,pad,kcap,now){
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9; const core=mode==="core";
  for(const n of ns){ const x=now?n.x:core?n.cx:n.nx, y=now?n.y:core?n.cy:n.ny; if(x<x0)x0=x; if(x>x1)x1=x; if(y<y0)y0=y; if(y>y1)y1=y; }
  if(core&&ns===G.nodes&&!now){ x0=y0=-CORE_R-40; x1=y1=CORE_R+40; }      /* room for the sector names */
  const k=clamp(Math.min((W-2*pad)/Math.max(1,x1-x0),(H-2*pad)/Math.max(1,y1-y0)),.04,kcap||KMAX);
  return {x:(x0+x1)/2,y:(y0+y1)/2,k};
}
function fitAll(){ const c=fit(G.nodes,26); kfit=c.k; return c; }
function zoomAt(f,sx,sy){
  const k=clamp(cam.k*f,kfit*.5,KMAX), wx=cam.x+(sx-W/2)/cam.k, wy=cam.y+(sy-H/2)/cam.k;
  cam={k,x:wx-(sx-W/2)/k,y:wy-(sy-H/2)/k}; camTo=null; userCam=true; touch();
}
function zoomBy(f){ if(!G) return; const c=camTo||cam; camTo={x:c.x,y:c.y,k:clamp(c.k*f,kfit*.5,KMAX)}; userCam=true; kick(); }
function flyTo(ns,pad,kcap){ if(ns.length){ camTo=fit(ns,pad||90,kcap||Math.max(kfit*5,1.8)); userCam=true; kick(); } }

/* ---------------------------------------------------------------- frame loop: base map only when it changes, overlay animates */
function frame(now){
  raf=0;
  if(view!=="wiki"||!G||document.hidden) return;
  if(resize()&&!userCam) camTo=fitAll();
  let moving=false;
  if(sim){ const t0=performance.now(); do simTick(); while(sim&&performance.now()-t0<9); moving=true; if(!userCam&&mode==="net") camTo=fitAll(); }
  const tc=mode==="core"?1:0;
  if(coreAmt!==tc){ coreAmt+=(tc-coreAmt)*.14; if(Math.abs(tc-coreAmt)<.004) coreAmt=tc; moving=true; }
  for(const n of G.nodes){ const tx=tc?n.cx:n.nx, ty=tc?n.cy:n.ny, dx=tx-n.x, dy=ty-n.y;
    if(dx*dx+dy*dy>.05){ n.x+=dx*.16; n.y+=dy*.16; moving=true; } else { n.x=tx; n.y=ty; } }
  if(camTo){ const dk=camTo.k-cam.k;
    cam.x+=(camTo.x-cam.x)*.15; cam.y+=(camTo.y-cam.y)*.15; cam.k+=dk*.15;
    if(Math.abs(dk)<cam.k*.002&&Math.abs(camTo.x-cam.x)*cam.k<.5&&Math.abs(camTo.y-cam.y)*cam.k<.5){ cam=camTo; camTo=null; }
    moving=true; }
  if(play&&now>=play.next) advance(now);
  if(moving||dirty){ project(); drawBase(); dirty=false; $("mczl").textContent=Math.round(cam.k/kfit*100)+"%"; }
  const anim=drawOverlay(now);
  if(moving||sim||play||(anim&&(anim===2||performance.now()-lastAct<20000))) kick();   /* idle flow stops after 20 s without the pointer */
}
function project(){
  const k=cam.k, ox=W/2-cam.x*k, oy=H/2-cam.y*k; O.x=ox; O.y=oy;
  for(const n of G.nodes){ n.sx=n.x*k+ox; n.sy=n.y*k+oy; n.sr=clamp(n.r*Math.pow(k,.55),1.3,24); n.vis=n.sx>-30&&n.sx<W+30&&n.sy>-30&&n.sy<H+30; }
}
/* a link from page a to page b, drawn to fraction p: straight in the network, bent towards the core in the ring layout */
function seg(ctx,a,b,p){
  const x1=a.sx,y1=a.sy,x2=b.sx,y2=b.sy;
  if(coreAmt<.02){ ctx.moveTo(x1,y1); ctx.lineTo(x1+(x2-x1)*p,y1+(y2-y1)*p); return; }
  const mx=(x1+x2)/2, my=(y1+y2)/2, qx=mx+(O.x-mx)*.45*coreAmt, qy=my+(O.y-my)*.45*coreAmt;
  ctx.moveTo(x1,y1);
  if(p>=1){ ctx.quadraticCurveTo(qx,qy,x2,y2); return; }
  const ax=x1+(qx-x1)*p, ay=y1+(qy-y1)*p, bx=qx+(x2-qx)*p, by=qy+(y2-qy)*p;
  ctx.quadraticCurveTo(ax,ay,ax+(bx-ax)*p,ay+(by-ay)*p);
}
function refocus(){
  let f=new Set();
  if(sel){ f.add(sel); sel.adj.forEach(a=>f.add(a[0])); }
  if(pathSel) pathSel.forEach(n=>f.add(n));
  probe.forEach(p=>p.nodes.forEach(n=>f.add(n)));
  if(stepHot!=null||stepMax<Infinity) shown().forEach(([s])=>s._nodes.forEach(n=>f.add(n)));
  focus=f.size?f:null;
  caption(); touch();
}

function drawBase(){
  const ctx=bctx, k=cam.k, N=G.nodes, F=focus, z=clamp(k/kfit,.5,6);
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H); placed=[]; ctx.lineCap="round";
  if(coreAmt>.01) drawCore(ctx,k);
  const off=(A,B)=>(A.sx<0&&B.sx<0)||(A.sx>W&&B.sx>W)||(A.sy<0&&B.sy<0)||(A.sy>H&&B.sy>H);
  if(showLinks){
    const d=(F?.3:1)*(1-.45*coreAmt), near=clamp((z-1.15)/1.1,0,1);   /* plain links fade in between 115 % and 225 % zoom */
    const al={l:Math.min(.45,.06+.05*z)*d*near,s:Math.min(.5,.1+.05*z)*d*near,r:Math.min(.7,.12+.08*z)*d};
    for(const kind of ["l","s","r"]){ if(al[kind]<.01) continue; ctx.beginPath();
      for(const [A,B] of G.ek[kind]) if(!off(A,B)) seg(ctx,A,B,1);
      ctx.strokeStyle=EDGE[kind][1]; ctx.globalAlpha=al[kind]; ctx.lineWidth=kind==="r"?1.1:.8; ctx.stroke(); }
  }
  if(F){   /* links inside what is in focus: around the selected page, else among everything highlighted */
    for(const kind of ["l","s","r"]){ ctx.beginPath();
      for(const [A,B] of G.ek[kind]) if(F.has(A)&&F.has(B)&&(!sel||A===sel||B===sel)&&!off(A,B)) seg(ctx,A,B,1);
      ctx.strokeStyle=EDGE[kind][1]; ctx.globalAlpha=sel?.9:.55; ctx.lineWidth=sel?1.6:1.1; ctx.stroke(); }
  }
  ctx.globalAlpha=1;
  const tick=coreAmt>.98, hmax=Math.max(1,...Object.values(heat));
  const shape=n=>{ if(tick){ const L=(13+Math.min(30,7*Math.log2(1+n.w)))*k, ca=Math.cos(n.a-Math.PI/2), sa=Math.sin(n.a-Math.PI/2);
      ctx.moveTo(n.sx-L*ca,n.sy-L*sa); ctx.lineTo(n.sx+L*ca,n.sy+L*sa); } else { ctx.moveTo(n.sx+n.sr,n.sy); ctx.arc(n.sx,n.sy,n.sr,0,TAU); } };
  const paint=col=>{ if(tick){ ctx.strokeStyle=col; ctx.lineWidth=Math.max(1.5,2.4*k); ctx.stroke(); }
    else { ctx.fillStyle=col; ctx.fill(); ctx.strokeStyle="#000"; ctx.lineWidth=1; ctx.stroke(); } };
  if(showHeat){ for(const n of N){ if(!n.vis) continue; const h=heat[n.i]||0; ctx.beginPath(); shape(n);
      ctx.globalAlpha=h?1:.5; paint(h?`hsl(${34-30*h/hmax},100%,${48+18*h/hmax}%)`:C.dim); } }
  else for(const dim of [true,false]) for(const ty of ORDER){
    ctx.beginPath(); let any=false;
    for(const n of N){ if(n.ty!==ty||!n.vis||(F?!F.has(n):false)!==dim) continue; any=true; shape(n); }
    if(any){ ctx.globalAlpha=dim?.2:1; paint(TYPES[ty][1]); } }
  ctx.globalAlpha=1;
  /* names: what is in focus first, then the heaviest pages that are big enough on screen; none may overlap */
  const forced=new Set([sel,...(pathSel||[])].filter(Boolean)), st=probe.length&&stepHot==null?[]:shown();
  courseOf(st).forEach(p=>forced.add(p.n)); st.forEach(([s])=>s._cites.forEach(n=>forced.add(n)));
  forced.forEach(n=>{ if(n.vis) label(ctx,n,n===sel?"#fff":TYPES[n.ty][1],13,true); });
  let left=F?30:0;
  if(F) for(const n of G.byW){ if(left<=0) break; if(F.has(n)&&n.vis&&label(ctx,n,TYPES[n.ty][1],12,false)) left--; }
  left=Math.round(clamp(8*Math.pow(z,1.5),8,60)); let dimLeft=F?Math.round(left*.15):0;
  for(const n of G.byW){ if(left<=0) break; if(!n.vis) continue;
    if(F&&!F.has(n)){ if(dimLeft>0&&label(ctx,n,"#7d6a4f",11,false)){ dimLeft--; left--; } }
    else if(label(ctx,n,TYPES[n.ty][1],11,false)) left--; }
}
function drawCore(ctx,k){
  const R=CORE_R*k, cx=O.x, cy=O.y; ctx.globalAlpha=coreAmt;
  ORDER.forEach(t=>{ ctx.beginPath(); ctx.arc(cx,cy,TYPES[t][2]*R,0,TAU); ctx.strokeStyle="#1a1409"; ctx.lineWidth=R*.085; ctx.stroke(); });
  ORDER.forEach(t=>{ ctx.beginPath(); ctx.arc(cx,cy,TYPES[t][2]*R+R*.047,0,TAU); ctx.strokeStyle="#2a1f10"; ctx.lineWidth=1; ctx.setLineDash([2,10]); ctx.stroke(); });
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(cx,cy,R*.1,0,TAU); ctx.fillStyle="#f90"; ctx.fill();
  ctx.beginPath(); ctx.arc(cx,cy,R*.065,0,TAU); ctx.fillStyle="#fc6"; ctx.fill();
  ctx.fillStyle="#000"; ctx.font=`700 ${Math.max(9,R*.03)}px Antonio,sans-serif`; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText("CORE",cx,cy);
  ctx.font=`600 ${clamp(R*.03,9,15)}px Antonio,sans-serif`; ctx.fillStyle="#a4855f";
  sectors.forEach(s=>{ if(!s) return; const a=s.a-Math.PI/2, r=R+20; ctx.save(); ctx.translate(cx+r*Math.cos(a),cy+r*Math.sin(a));
    let rot=a+Math.PI/2; if(rot>Math.PI/2&&rot<Math.PI*1.5) rot+=Math.PI; ctx.rotate(rot); ctx.fillText(s.t.toUpperCase(),0,0); ctx.restore(); });
  ctx.globalAlpha=1;
}
/* one name per page, never over another name: right of the dot, else left, below, above; a must-have name is stacked instead */
function label(ctx,n,col,size,must){
  if(placed.some(r=>r[4]===n)) return false;
  const s=n.t.length>38?n.t.slice(0,37)+"…":n.t;
  ctx.font=`${must?700:600} ${size}px Antonio,sans-serif`;
  const w=ctx.measureText(s).width, h=size+3, g=n.sr+5;
  const free=(x,y)=>!placed.some(r=>x<r[2]&&x+w>r[0]&&y<r[3]&&y+h>r[1]);
  let pos=[[n.sx+g,n.sy-h/2],[n.sx-g-w,n.sy-h/2],[n.sx-w/2,n.sy+g-2],[n.sx-w/2,n.sy-g-h+2]].find(p=>free(p[0],p[1]));
  if(!pos&&must) for(const d of [17,-17,34,-34,51,-51]){ if(free(n.sx+g,n.sy-h/2+d)){ pos=[n.sx+g,n.sy-h/2+d]; break; } }
  if(!pos) return false;
  placed.push([pos[0]-2,pos[1]-1,pos[0]+w+2,pos[1]+h+1,n]);
  ctx.textAlign="left"; ctx.textBaseline="top"; ctx.lineJoin="round"; ctx.lineWidth=3.5; ctx.strokeStyle="rgba(0,0,0,.9)";
  ctx.strokeText(s,pos[0],pos[1]+1); ctx.fillStyle=col; ctx.fillText(s,pos[0],pos[1]+1);
  return true;
}

/* ---------------------------------------------------------------- overlay: hover, path, probe, Computer's session */
function ring(ctx,n,r,col,w,dash,alpha){ ctx.beginPath(); ctx.arc(n.sx,n.sy,r,0,TAU); ctx.strokeStyle=col; ctx.lineWidth=w||1.5; ctx.setLineDash(dash||[]);
  ctx.globalAlpha=alpha==null?1:alpha; ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha=1; }
function pillAt(ctx,x,y,text,col,alpha){
  ctx.font="700 12px Antonio,sans-serif"; const w=ctx.measureText(text).width+18, r=10;
  ctx.beginPath(); ctx.moveTo(x-w/2+r,y-r); ctx.arc(x+w/2-r,y,r,-Math.PI/2,Math.PI/2); ctx.arc(x-w/2+r,y,r,Math.PI/2,Math.PI*1.5); ctx.closePath();
  const a=alpha==null?1:alpha;
  ctx.globalAlpha=.9*a; ctx.lineWidth=4; ctx.strokeStyle="#000"; ctx.stroke(); ctx.globalAlpha=a; ctx.fillStyle=col; ctx.fill();
  ctx.fillStyle="#000"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(text,x,y+1); ctx.globalAlpha=1;
}
/* term pills in a row above the pages they point at; returns their x positions and the row's y */
function pillRow(ctx,terms,nodes,lift,clear){
  let ax=0, ay=1e9; nodes.forEach(n=>{ ax+=n.sx; ay=Math.min(ay,n.sy); }); ax/=nodes.length;
  (clear||[]).forEach(n=>{ if(n.vis) ay=Math.min(ay,n.sy); });
  ctx.font="700 12px Antonio,sans-serif";
  const ws=terms.map(t=>ctx.measureText(t.toUpperCase()).width+18), tot=ws.reduce((a,b)=>a+b+8,-8);
  let x=clamp(ax-tot/2,6,Math.max(6,W-tot-6)); const y=clamp(ay-44-lift,16,H-16);
  return {y,xs:terms.map((t,i)=>{ const c=x+ws[i]/2; x+=ws[i]+8; return c; })};
}
function drawOverlay(now){
  const ctx=octx; ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H); ctx.lineCap="round";
  if(!G) return 0;
  let anim=0; const flow=-(now/38)%13, st=shown(), t=trailOf();
  const prog=k=>{ const a=stepAt[k]; if(a==null) return 1; const p=clamp((now-a)/(900/speed),0,1); if(p<1) anim=2; return p; };
  const flowing=(col,w,alpha)=>{ ctx.strokeStyle=col; ctx.lineWidth=w; ctx.globalAlpha=alpha; ctx.setLineDash([7,6]); ctx.lineDashOffset=flow; ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha=1; anim=anim||1; };
  const olab=[];   /* names the overlay adds on top of the base map's */
  /* probe: each term wired to the pages that contain it */
  if(probe.length&&stepHot==null){
    const row=probe.filter(p=>p.nodes.length);
    row.forEach((p,i)=>{ const top=p.nodes.slice(0,26), r=pillRow(ctx,[p.term],top,i*26); p._x=r.xs[0]; p._y=r.y;
      ctx.beginPath(); top.forEach(n=>{ ctx.moveTo(p._x,p._y); ctx.lineTo(n.sx,n.sy); }); flowing(p.color,1.1,.55);
      p.nodes.forEach(n=>{ if(n.vis) ring(ctx,n,n.sr+3+2*n._tm.indexOf(p),p.color,1.6); }); });
    probe.shared.slice(0,14).forEach(n=>{ if(n.vis){ ring(ctx,n,n.sr+9,"#fff",2); olab.push([n,"#fff"]); } });
    row.forEach(p=>pillAt(ctx,p._x,p._y,p.term.toUpperCase()+"  "+p.nodes.length,p.color));
  }
  /* Computer's session */
  if(t&&st.length&&!(probe.length&&stepHot==null)){
    const last=stepMax<Infinity&&stepHot==null?stepMax-1:stepHot; let si=0;
    for(const [s,k] of st){ const p=prog(k);
      if(s.tool==="overview"&&k===last&&p<1){   /* catalogue scan: one sweep over the whole map */
        const a=p*TAU-Math.PI/2, R=Math.hypot(W,H); ctx.beginPath(); ctx.moveTo(W/2,H/2); ctx.arc(W/2,H/2,R,a-.5,a); ctx.closePath();
        ctx.fillStyle="rgba(255,204,102,.10)"; ctx.fill(); ctx.beginPath(); ctx.moveTo(W/2,H/2); ctx.lineTo(W/2+R*Math.cos(a),H/2+R*Math.sin(a));
        ctx.strokeStyle="rgba(255,204,102,.7)"; ctx.lineWidth=2; ctx.stroke(); }
      if(s.tool==="search"&&s._hits.length){
        const full=k===last, direct=s._hits.filter(h=>!h.via&&(full||!h.text)), entry=direct.filter(h=>!h.text), T=s._terms, p2=clamp(p*2-1,0,1);
        const row=T.length&&s._hits.some(h=>h.n.vis)?pillRow(ctx,T,(entry.length?entry:s._hits).map(h=>h.n),26*si++,s._nodes):null, used=[];
        if(row) T.forEach((term,i)=>{ for(const text of [false,true]){ ctx.beginPath(); let any=false;
            direct.forEach(h=>{ if(h.text!==text||!(has(h.n,term)||!T.some(q=>has(h.n,q)))) return; any=true;
              ctx.moveTo(row.xs[i],row.y); ctx.lineTo(row.xs[i]+(h.n.sx-row.xs[i])*p,row.y+(h.n.sy-row.y)*p); });
            if(!any) continue;
            used[i]=1;
            if(text){ ctx.strokeStyle=TERMC[i%6]; ctx.lineWidth=1.2; ctx.globalAlpha=.7; ctx.setLineDash([1.5,5]); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha=1; }
            else flowing(TERMC[i%6],1.5,.85); } });
        if(full){ ctx.beginPath(); s._hits.forEach(h=>{ if(h.via&&h.via!==h.n) seg(ctx,h.via,h.n,p2); }); flowing(C.rel,1.4,.85*Math.min(1,p2*3)); }
        s._hits.forEach(h=>{ if(full||(!h.via&&!h.text)) ring(ctx,h.n,h.n.sr+(h.via?3.5:5),h.via?C.rel:C.hit,h.via?1.3:1.8,h.text?[2,3]:null,h.via?p2:p); });
        if(row) T.forEach((term,i)=>pillAt(ctx,row.xs[i],row.y,term.toUpperCase(),TERMC[i%6],used[i]?1:.4));
        if(k===last||stepHot===k) s._hits.slice(0,12).forEach(h=>olab.push([h.n,h.via?C.rel:C.hit]));
      }
      if(s.tool==="related"&&s._n&&k===last){
        ctx.beginPath(); s._hits.forEach(h=>seg(ctx,s._n,h.n,p)); flowing(C.rel,1.5,.85);
        s._hits.forEach(h=>ring(ctx,h.n,h.n.sr+3.5,C.rel,1.3,null,p)); ring(ctx,s._n,s._n.sr+6,C.rel,2.2);
        if(k===last||stepHot===k) s._hits.slice(0,12).forEach(h=>olab.push([h.n,C.rel]));
      }
      s._cites.forEach(n=>ring(ctx,n,n.sr+9,C.cite,2.4,null,p));
    }
    const course=courseOf(st);
    for(let i=1;i<course.length;i++){ ctx.beginPath(); seg(ctx,course[i-1].n,course[i].n,prog(course[i].k)); flowing(C.course,2.8,stepHot!=null?.4:.95); }
    let num=0;
    course.forEach(({n,k,s})=>{ const read=s.tool==="read"; if(read) num++;
      if(read&&k===last){ ring(ctx,n,n.sr+10+4*Math.sin(now/220),C.readRing,2,null,.6); anim=anim||1; }
      const r=Math.max(7,n.sr+2); ctx.beginPath(); ctx.arc(n.sx,n.sy,r,0,TAU); ctx.fillStyle=read?C.read:"#000"; ctx.fill();
      ctx.strokeStyle=read?C.readRing:C.course; ctx.lineWidth=2.4; ctx.stroke();
      if(read){ ctx.font="700 10px Antonio,sans-serif"; ctx.fillStyle="#000"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(String(num),n.sx,n.sy+.5); } });
  }
  /* shortest path between two pages */
  if(pathSel&&pathSel.length>1){ ctx.beginPath(); for(let i=1;i<pathSel.length;i++) seg(ctx,pathSel[i-1],pathSel[i],1);
    ctx.strokeStyle="rgba(0,0,0,.8)"; ctx.lineWidth=6; ctx.stroke(); ctx.beginPath(); for(let i=1;i<pathSel.length;i++) seg(ctx,pathSel[i-1],pathSel[i],1); flowing(C.path,3,1);
    pathSel.forEach((n,i)=>ring(ctx,n,n.sr+5,C.path,i===0||i===pathSel.length-1?2.6:1.6)); }
  if(sel) ring(ctx,sel,sel.sr+6,"#fff",2.2);
  /* hover: this page's links, coloured by relation, and its neighbours' names */
  if(hov&&hov.sx!=null){
    for(const kind of ["l","s","r"]){ ctx.beginPath(); hov.adj.forEach(([m,kk])=>{ if(kk===kind) seg(ctx,hov,m,1); });
      ctx.strokeStyle=EDGE[kind][1]; ctx.lineWidth=1.6; ctx.globalAlpha=.95; ctx.stroke(); }
    ctx.globalAlpha=1; ring(ctx,hov,hov.sr+4,"#fff",1.6);
    olab.unshift([hov,"#fff"]); [...hov.adj].sort((a,b)=>b[0].w-a[0].w).slice(0,16).forEach(([m,kk])=>olab.push([m,EDGE[kk][1]]));
  }
  const keep=placed; placed=keep.slice();
  olab.forEach(([n,col],i)=>{ if(n.vis) label(ctx,n,col,12,i===0&&n===hov); });
  placed=keep;
  return anim;
}

/* ---------------------------------------------------------------- pointer: pan, zoom, pick */
function pick(x,y){
  if(!G) return null; let best=null, d=1e9;
  for(const n of G.nodes){ if(!n.vis) continue; const r=Math.max(9,n.sr+4), q=(n.sx-x)**2+(n.sy-y)**2; if(q<r*r&&q<d){ d=q; best=n; } }
  return best;
}
function pointer(ov){
  const ptr=new Map(); let drag=null;
  const xy=e=>{ const b=ov.getBoundingClientRect(); return [e.clientX-b.left,e.clientY-b.top]; };
  const span=()=>{ const [a,b]=[...ptr.values()]; return [Math.hypot(a[0]-b[0],a[1]-b[1]),(a[0]+b[0])/2,(a[1]+b[1])/2]; };
  ov.addEventListener("pointerdown",e=>{ try{ ov.setPointerCapture(e.pointerId); }catch(_){}
    ptr.set(e.pointerId,xy(e)); drag={moved:false,d:ptr.size===2?span()[0]:0}; });
  ov.addEventListener("pointermove",e=>{
    const p=xy(e), q=ptr.get(e.pointerId);
    if(!q){ const n=pick(p[0],p[1]); if(n!==hov){ hov=n; kick(); } ov.classList.toggle("pt",!!n); tip(p,n); return; }
    ptr.set(e.pointerId,p);
    if(ptr.size===2){ const [d,mx,my]=span(); if(drag.d>0) zoomAt(d/drag.d,mx,my); drag.d=d; drag.moved=true; return; }
    const dx=p[0]-q[0], dy=p[1]-q[1];
    if(!drag.moved&&Math.hypot(dx,dy)<3){ ptr.set(e.pointerId,q); return; }
    drag.moved=true; ov.classList.add("drag"); tip(null,null);
    cam={k:cam.k,x:cam.x-dx/cam.k,y:cam.y-dy/cam.k}; camTo=null; userCam=true; touch();
  });
  const up=e=>{ const had=ptr.delete(e.pointerId); ov.classList.remove("drag");
    if(had&&drag&&!drag.moved&&ptr.size===0&&e.type==="pointerup"){ const p=xy(e), n=pick(p[0],p[1]);
      if(n&&sel&&n!==sel&&(e.shiftKey||pathMode)) setPath(sel,n); else if(!(pathMode&&!n)) select(n); }   /* a miss in path mode keeps the selection */
    if(ptr.size<2&&drag) drag.d=0; };
  ov.addEventListener("pointerup",up); ov.addEventListener("pointercancel",up);
  ov.addEventListener("pointerleave",()=>{ if(hov){ hov=null; kick(); } tip(null,null); });
  ov.addEventListener("wheel",e=>{ e.preventDefault(); const p=xy(e); zoomAt(Math.exp(-e.deltaY*(e.ctrlKey?.012:.0016)*(e.deltaMode?18:1)),p[0],p[1]); },{passive:false});
  ov.addEventListener("dblclick",e=>{ const p=xy(e), n=pick(p[0],p[1]); if(n){ select(n,true); } else zoomAt(1.8,p[0],p[1]); });
}
function tip(p,n){
  const el=$("mctip"); if(!n||!p){ el.style.display="none"; return; }
  el.innerHTML=`${esc(n.t)}<small>${esc(ONE[n.ty])} · ${n.adj.length} links${heat[n.i]?" · consulted":""}</small>`;
  el.style.display="block"; el.style.left=Math.min(p[0]+14,W-el.offsetWidth-4)+"px"; el.style.top=Math.min(p[1]+16,H-26)+"px";
}

/* ---------------------------------------------------------------- selection, path, probe */
function select(n,fly){
  sel=n||null; pathSel=null; pathMode=false; $("mcov").classList.remove("path");
  if(docN&&docN!==sel) sel?openDoc(sel):closeDoc();   /* an open page follows the selection */
  if(fly&&n) flyTo([n,...n.adj.map(a=>a[0])],90);
  paintCard(); refocus();
}
function setPath(a,b){   /* breadth-first over links in either direction: the fewest hops between two pages */
  const prev=new Map([[a,null]]), q=[a];
  for(let i=0;i<q.length&&!prev.has(b);i++) for(const [m] of q[i].adj) if(!prev.has(m)){ prev.set(m,q[i]); q.push(m); }
  pathMode=false; $("mcov").classList.remove("path");
  if(!prev.has(b)){ pathSel=[]; paintCard(b); refocus(); return; }
  const p=[]; for(let n=b;n;n=prev.get(n)) p.unshift(n);
  pathSel=p; flyTo(p,110); paintCard(b); refocus();
}
function setProbe(q){
  const T=termsOf(q); probe=[]; probe.shared=[]; probe.links=0;
  if(G&&T.length){
    G.nodes.forEach(n=>n._tm=null);
    probe=T.map((term,i)=>{ const p={term,color:TERMC[i%6],nodes:[]};
      G.nodes.forEach(n=>{ const a=n.lt.includes(term); if(a||n.ls.includes(term)){ n._sc=(a?1000:0)+n.w; p.nodes.push(n); (n._tm=n._tm||[]).push(p); } });
      p.nodes.sort((a,b)=>b._sc-a._sc); return p; });
    const all=new Set(probe.flatMap(p=>p.nodes));
    probe.shared=[...all].filter(n=>n._tm.length>1).sort((a,b)=>b._tm.length-a._tm.length||b.w-a.w);
    probe.links=T.length>1?G.edges.filter(([u,v])=>{ const a=G.nodes[u]._tm, b=G.nodes[v]._tm; return a&&b&&all.has(G.nodes[u])&&all.has(G.nodes[v])&&a.some(x=>!b.includes(x)); }).length:0;
    const top=[...new Set(probe.flatMap(p=>p.nodes.slice(0,26)))];
    if(top.length) flyTo(top,80,Math.max(kfit*3,1.2));
  }
  paintCard(); refocus();
}

/* ---------------------------------------------------------------- replay */
function nodesFor(k){ const t=trailOf(), ns=new Set(t.steps[k]._nodes); const c=courseOf(t.steps.slice(0,k).map((s,i)=>[s,i])); if(c.length) ns.add(c[c.length-1].n); return [...ns]; }
function showStep(k,now){
  const t=trailOf(); if(!t) return;
  stepMax=k+1; stepAt={[k]:now||performance.now()};
  if(follow){ const ns=nodesFor(k); if(t.steps[k].tool==="overview"||!ns.length){ if(t.steps[k].tool!=="web"&&t.steps[k].tool!=="answer"){ camTo=fitAll(); } }
    else camTo=fit(ns,110,Math.max(kfit*4.5,1.6)); userCam=true; }
  paintLog(); paintTl(); refocus();
}
function advance(now){
  const t=trailOf(); if(!t||stepMax>=t.steps.length){ stopPlay(); return; }
  showStep(stepMax===Infinity?0:stepMax,now); play.next=now+1700/speed;
}
function stopPlay(){ play=null; paintTl(); }
function replay(){
  const t=trailOf(); if(!t) return;
  if(play){ stopPlay(); return; }
  if(stepMax>=t.steps.length) stepMax=0;
  play={next:0}; paintTl(); kick();
}
function showAll(){ stopPlay(); stepMax=Infinity; stepAt={}; paintLog(); paintTl(); refocus(); const t=trailOf(); if(t&&follow){ const ns=[...new Set(t.steps.flatMap(s=>s._nodes))]; if(ns.length) flyTo(ns,100); } }

/* ---------------------------------------------------------------- panels */
function stepText(s){
  const n=s._n, via=s._hits.filter(h=>h.via).length, txt=s._hits.filter(h=>h.text).length;
  let main="", sub="";
  if(s.tool==="ask") main=s.arg;
  else if(s.tool==="overview"){ main="Catalogue scan"; sub=s.pages?`${s.pages} pages`:""; }
  else if(s.tool==="search"){ main=`“${s.arg}”`; sub=`${s._hits.length} hits${via?` · ${via} via relations`:""}${txt?` · ${txt} full text`:""}`; }
  else if(s.tool==="read"||s.tool==="related"){ main=n?n.t:s.arg; sub=s.tool==="related"?`${s._hits.length} neighbours · depth ${s.depth||1}`:(n?ONE[n.ty]:"not in graph"); }
  else if(s.tool==="answer"){ const c=s._cites; main=c.length?`Cites ${c.length} page${c.length>1?"s":""}`:"No page cited"; sub=c.slice(0,4).map(n=>n.t).join(" · "); }
  else if(s.tool==="web"){ main=`${s.name}: ${s.arg}`; sub=s.blocked?"Privacy lock: blocked after a wiki read":"left the ship"; }
  const lock=s.tool==="web"&&s.blocked;
  return {main,sub,lbl:lock?"Locked":KIND[s.tool]||s.tool,cls:lock?"k-lock":"k-"+s.tool};
}
function caption(){
  const el=$("mccap"), t=trailOf(), k=stepHot!=null?stepHot:stepMax<Infinity?stepMax-1:null;
  if(!t||k==null||!t.steps[k]){ el.style.display="none"; return; }
  const x=stepText(t.steps[k]);
  el.innerHTML=`<i class="${x.cls}">${String(k+1).padStart(2,"0")} ${esc(x.lbl)}</i><span>${esc(x.main)}${x.sub?`<small>${esc(x.sub)}</small>`:""}</span>`;
  el.style.display="flex";
}
function nbList(n){
  const g={r:new Map(),l:new Map(),s:new Map()};
  n.adj.forEach(([m,k,out])=>{ const d=g[k]||g.l; d.set(m,(d.get(m)||0)|(out?1:2)); });
  return ["r","l","s"].filter(k=>g[k].size).map(k=>`<div class="nbh" style="color:${EDGE[k][1]}">${EDGE[k][0]} · ${g[k].size}</div><div class="nb">`+
    [...g[k]].sort((a,b)=>b[0].w-a[0].w).slice(0,30).map(([m,d])=>`<a data-p="${esc(m.p)}">${["","→","←","↔"][d]} ${esc(m.t)}</a>`).join("")+
    (g[k].size>30?`<small class="mcempty">${g[k].size-30} more</small>`:"")+`</div>`).join("");
}
function paintCard(pathTo){
  if(!built) return;
  const pr=$("mcprobe"), el=$("mcpage");
  if(probe.length){
    pr.style.display="block";
    pr.innerHTML=`<h3>Term probe<span><button type="button" id="mcprx">Clear</button></span></h3>
      <div class="kv">${probe.map(p=>`<span class="t" style="background:${p.color}">${esc(p.term)}<b>${p.nodes.length}</b></span>`).join("")}</div>
      ${probe.length>1?`<p>${probe.shared.length} page${probe.shared.length===1?" contains":"s contain"} more than one term; ${probe.links} link${probe.links===1?"":"s"} join pages of different terms.</p>
        <div class="nb">${probe.shared.slice(0,14).map(n=>`<a data-p="${esc(n.p)}">${esc(n.t)}<small>${n._tm.map(p=>esc(p.term)).join(" + ")}</small></a>`).join("")}</div>`
       :`<div class="nb">${(probe[0].nodes||[]).slice(0,14).map(n=>`<a data-p="${esc(n.p)}">${esc(n.t)}<small>${esc(ONE[n.ty])}</small></a>`).join("")}</div>`}
      <p class="d">A page matches when its title, tags or summary contain the term.</p>`;
    $("mcprx").onclick=()=>{ $("mcq").value=""; setProbe(""); };
  } else pr.style.display="none";
  if(!sel){ el.style.display="none"; }
  else{
    const n=sel, col=TYPES[n.ty][1];
    const reads=trails.reduce((a,t)=>a+t.steps.filter(s=>s.tool==="read"&&s._n===n).length,0);
    const kindOf=(a,b)=>{ const e=a.adj.find(x=>x[0]===b); return e?EDGE[e[1]][0]+(e[2]?" →":" ←"):""; };
    const path=pathSel?(pathSel.length>1?`<div class="nbh" style="color:#fff">Shortest path · ${pathSel.length-1} hop${pathSel.length>2?"s":""}</div><div class="nb path">`+
        pathSel.map((m,i)=>`<a data-p="${esc(m.p)}">${esc(m.t)}${i?`<small>${esc(kindOf(pathSel[i-1],m))}</small>`:""}</a>`).join("")+`</div>`
      :`<div class="nbh" style="color:var(--red)">No chain of links joins this page and ${esc(pathTo?pathTo.t:"that page")}.</div>`):"";
    el.style.display="block"; el.style.borderLeftColor=col;
    el.innerHTML=`<h3 style="color:${col}">${esc(n.t)}<span><button type="button" id="mcpo" class="${docN===n?"on":""}" title="Read this page's markdown">Open</button><button type="button" id="mcpf" title="Zoom to this page and its neighbours">Focus</button><button
        type="button" id="mcpp" class="${pathMode?"on":""}" title="Then click a second page: the shortest chain of links between the two (or Shift-click it)">Path to…</button><button type="button" id="mcpx">Close</button></span></h3>
      <div class="pth">${esc(n.type||"untyped")} · ${esc(n.p)}</div>${n.sum?`<p>${esc(n.sum)}</p>`:""}
      <div class="tg">${n.tags.map(t=>`<u title="Probe this tag">${esc(t)}</u>`).join("")}</div>
      <div class="kv"><span>Links<b>${n.adj.length}</b></span><span>Weight<b>${n.w}</b></span><span>Read by Computer<b>${reads}</b></span></div>
      ${path}${nbList(n)}`;
    $("mcpx").onclick=()=>select(null);
    $("mcpo").onclick=()=>docN===n?closeDoc():openDoc(n);
    $("mcpf").onclick=()=>flyTo([n,...n.adj.map(a=>a[0])],90);
    $("mcpp").onclick=()=>{ pathMode=!pathMode; $("mcpp").classList.toggle("on",pathMode); $("mcov").classList.toggle("path",pathMode); };
    el.querySelectorAll(".tg u").forEach(u=>u.onclick=()=>{ $("mcq").value=u.textContent; setProbe(u.textContent); });
  }
  shape();
  document.querySelectorAll("#mcr .nb a").forEach(a=>{ const m=byPath[a.dataset.p]; if(!m) return;
    a.onclick=()=>select(m,!m.vis); a.onmouseenter=()=>{ hov=m; kick(); }; a.onmouseleave=()=>{ hov=null; kick(); }; });
}
/* Open: the selected page's markdown in a panel over the map, read from disk by /api/computer/wiki/page (works
   while the memory-core service is stopped). Frontmatter folds into "Properties"; [[wiki links]] select that page. */
let docN=null, docSeq=0, docText="", docSha="", docEd=null;   /* docEd: {stem, text} the editor started from */
function docDirty(){ return docEd&&$("mcen")&&($("mcen").value.trim()!==docEd.stem||$("mcet").value!==docEd.text); }
function docLeave(){ return !docDirty()||confirm("Discard your unsaved changes to this page?"); }
function closeDoc(){ if(!docLeave()) return; docEd=null; docN=null; docSeq++; const d=$("mcdoc"); if(d) d.remove(); if(sel) paintCard(); }
async function openDoc(n){
  if(!docLeave()) return; docEd=null;
  let d=$("mcdoc");
  if(!d){
    d=document.createElement("div"); d.id="mcdoc";
    d.addEventListener("click",e=>{ const a=e.target.closest("a[href^='#wiki:']"); if(!a) return;
      e.preventDefault(); const m=resolve(decodeURIComponent(a.getAttribute("href").slice(6))); if(m) select(m,!m.vis); });
    $("mcbox").appendChild(d);
  }
  docN=n; const seq=++docSeq, col=TYPES[n.ty][1];
  d.style.borderLeftColor=col;
  d.innerHTML=`<div class="hd"><b style="color:${col}">${esc(n.t)}</b><button type="button" id="mcde" title="Edit this page's name and markdown">Edit</button><button type="button" id="mcdx" title="Close (Esc)">Close</button></div>
    <div class="pth">${esc(n.p)}</div><div class="cm"><div class="body"><span class="st loading">loading...</span></div></div>`;
  $("mcdx").onclick=closeDoc; $("mcde").disabled=true; if(sel) paintCard();
  let text;
  try{ const j=await compJSON("/api/computer/wiki/page?p="+encodeURIComponent(n.p)); text=j.md; if(seq===docSeq){ docText=j.md; docSha=j.sha; } }
  catch(e){ if(seq===docSeq) d.querySelector(".body").innerHTML=`<span class="err">${esc(e.message||String(e))}</span>`; return; }
  if(seq!==docSeq) return;                                   /* another page was opened meanwhile */
  $("mcde").disabled=false; $("mcde").onclick=editDoc;
  const fm=/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text), body=(fm?text.slice(fm[0].length):text)
    .replace(/!\[\[/g,"[[")
    .replace(/\[\[([^\]|#\n]+)(#[^\]|\n]*)?(?:\|([^\]\n]+))?\]\]/g,(m,t,h,a)=>`[${(a||t+(h||"")).replace(/[[\]]/g,"")}](#wiki:${encodeURIComponent(t.trim())})`);
  const b=d.querySelector(".body");
  b.innerHTML=(fm?`<details class="fm"><summary>Properties</summary><pre>${esc(fm[1])}</pre></details>`:"")+md(body);
  b.querySelectorAll("a[href^='#wiki:']").forEach(a=>{ a.removeAttribute("target"); a.classList.toggle("dead",!resolve(decodeURIComponent(a.getAttribute("href").slice(6)))); });
  if(window.compHL) compHL(b);
}
/* Edit: rename the file and/or change its markdown. Saved by /api/computer/wiki/edit (wikiedit.py, memory-core's locks):
   a rename also fixes every [[link]] to the page; a page changed on disk since it was opened is refused, not overwritten. */
function editDoc(){
  const n=docN, d=$("mcdoc"); if(!n||!d) return;
  const stem=n.p.split("/").pop().replace(/\.md$/,""), dir=n.p.slice(0,n.p.length-stem.length-3);
  docEd={stem, text:docText};
  $("mcde").hidden=true;
  d.querySelector(".cm").innerHTML=`<div class="ed">
    <label>File name <span class="dir">${esc(dir)}</span><input id="mcen" spellcheck="false" autocomplete="off"><span class="dir">.md</span></label>
    <small>Renaming fixes every [[link]] to this page. The title shown on the map is <code>title:</code> in the properties below.</small>
    <textarea id="mcet" spellcheck="false"></textarea>
    <div class="eb"><span id="mces"></span><button type="button" id="mcec">Cancel</button><button type="button" id="mcev" class="go">Save</button></div></div>`;
  $("mcen").value=stem; $("mcet").value=docText; $("mcet").focus();
  $("mcec").onclick=()=>{ if(docLeave()){ docEd=null; openDoc(n); } };
  $("mcev").onclick=saveDoc;
  d.querySelector(".ed").addEventListener("keydown",e=>{ if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); saveDoc(); }
    else if(e.key==="Escape"){ e.stopPropagation(); $("mcec").click(); } });
}
async function saveDoc(){
  const n=docN; if(!n||!docEd) return;
  const stem=$("mcen").value.trim(), text=$("mcet").value, st=$("mces");
  if(!stem){ st.textContent="The file name is empty."; st.className="err"; return; }
  if(stem===docEd.stem&&text===docEd.text){ docEd=null; return openDoc(n); }
  $("mcev").disabled=true; st.textContent="saving..."; st.className="";
  try{
    const r=await compJSON("/api/computer/wiki/edit",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({rel:n.p, sha:docSha, text:text===docEd.text?null:text, stem:stem===docEd.stem?null:stem})});
    docEd=null;
    await openDoc(Object.assign({},n,{p:r.rel}));
    const k=(r.relinked||[]).length, pth=$("mcdoc")&&$("mcdoc").querySelector(".pth");
    if(pth) pth.textContent+=` · saved${r.rel!==n.p?`, renamed, ${k} linking page${k===1?"":"s"} updated`:""}`;
    const back=()=>{ gAt=0; loadGraph().then(()=>{ const x=byPath[r.rel]; if(x&&docN&&docN.p===r.rel){ docN=x; select(x); } }); };
    back(); setTimeout(back,6000);                            /* the server re-exports the map in the background */
  }catch(e){ if($("mces")){ st.textContent=e.message; st.className="err"; $("mcev").disabled=false; } }
}
addEventListener("keydown",e=>{ if(e.key==="Escape"&&docN&&view==="wiki"&&!/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) closeDoc(); });

/* Raw: the Knowledge DB's .raw folder through memory-core's wiki_ingest_raw (/api/computer/wiki/raw and /ingest). An ingest only
   PROPOSES: each new file becomes a change that is applied on its review page; the local model handles one file at a time. */
let rawRes="", rawBusy=false;
const rawPost=(u,b)=>window.compPost?window.compPost(u,b):compJSON(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
function closeRaw(){ const d=$("mcraw"); if(d) d.remove(); $("mcrawb").classList.remove("on"); }
async function openRaw(){
  let d=$("mcraw");
  if(!d){ d=document.createElement("div"); d.id="mcraw"; d.style.borderLeftColor="var(--orange)"; $("mcbox").appendChild(d); }
  $("mcrawb").classList.add("on");
  d.innerHTML=`<div class="hd"><b style="color:var(--orange)">Raw sources</b><button type="button" id="mcrwi" disabled>${rawBusy?"Ingesting...":"Ingest"}</button><button
      type="button" id="mcrwr">Refresh</button><button type="button" id="mcrwx" title="Close (Esc)">Close</button></div>
    <div class="pth">Knowledge DB · .raw · an ingest only proposes: apply each file on its review page</div>
    <div class="cm"><div class="body">${rawRes?md(rawRes)+"<hr>":""}<span class="st loading">reading .raw...</span></div></div>`;
  $("mcrwx").onclick=closeRaw; $("mcrwr").onclick=openRaw;
  let j;
  try{ j=await compJSON("/api/computer/wiki/raw"); }
  catch(e){ if($("mcraw")) d.querySelector(".body").innerHTML=`<span class="err">${esc(e.message||String(e))}</span>`; return; }
  if(!$("mcraw")) return;
  const n=j.counts?j.counts.new:0, b=$("mcrwi");
  b.disabled=rawBusy||!n||j.error; b.textContent=rawBusy?"Ingesting...":n?`Ingest ${Math.min(n,5)} new`:"Nothing new";
  b.title=n?"Queue up to 5 new files; each becomes a proposal with a review link":""; b.onclick=ingestRaw;
  d.querySelector(".body").innerHTML=(rawRes?md(rawRes)+"<hr>":"")+(j.error?`<span class="err">${esc(j.text)}</span>`:md(j.text));
}
async function ingestRaw(){
  if(rawBusy) return;
  rawBusy=true; const b=$("mcrwi"); if(b){ b.disabled=true; b.textContent="Ingesting..."; }
  try{ const r=await rawPost("/api/computer/wiki/ingest",{limit:5}); rawRes=r.text||""; }
  catch(e){ rawRes="**Ingest failed:** "+(e.message||String(e)); }
  rawBusy=false; if($("mcraw")) openRaw();
}
addEventListener("keydown",e=>{ if(e.key==="Escape"&&$("mcraw")&&view==="wiki"&&!/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) closeRaw(); });

function funnel(t){
  const S=t.steps, u=a=>new Set(a.filter(Boolean)).size;
  return {search:S.filter(s=>s.tool==="search").length, hits:u(S.flatMap(s=>s.tool==="search"?s._hits.map(h=>h.n):[])),
    related:S.filter(s=>s.tool==="related").length, read:u(S.filter(s=>s.tool==="read").map(s=>s._n)), cited:u(S.flatMap(s=>s._cites)),
    lock:S.filter(s=>s.blocked).length};
}
/* the session drawn on the map; "" = none (Clear, or tapping the shown session again). Kept across reloads. */
function pickChat(id){ chatSel=id; LS.set("mcNoChat",id===""); stepHot=null; if(play) stopPlay(); showAll(); paintLists();
  if(id===""&&G){ userCam=false; camTo=fitAll(); touch(); } }   /* off: back to the whole map */
function paintLists(){
  if(!built) return;
  $("mcchats").innerHTML=trails.length?trails.map(t=>{ const f=funnel(t);
      return `<div class="mci ${t.id===chatSel?"on":""}" data-id="${esc(t.id)}">${esc(t.title)}
        <small>${f.search} search · ${f.hits} hits · ${f.read} read · ${f.cited} cited · ${ago(t.updated)}</small></div>`; }).join("")
    :`<div class="mcempty">No Computer session has used the wiki yet. Ask Computer about your notes; its search shows here.</div>`;
  $("mcchats").querySelectorAll(".mci").forEach(el=>el.onclick=()=>pickChat(el.dataset.id===chatSel?"":el.dataset.id));   /* tap the shown session again: off */
  const tc={}; trails.forEach(t=>t.steps.forEach(s=>(s._terms||[]).forEach(x=>tc[x]=(tc[x]||0)+1)));
  const terms=Object.entries(tc).sort((a,b)=>b[1]-a[1]).slice(0,18);
  $("mcterms").innerHTML=terms.length?terms.map(([x,c])=>`<button type="button" class="mcterm" data-t="${esc(x)}" title="Probe this term on the map">${esc(x)}<b>${c}</b></button>`).join("")
    :`<div class="mcempty">The words Computer searched for appear here.</div>`;
  $("mcterms").querySelectorAll(".mcterm").forEach(b=>b.onclick=()=>{ $("mcq").value=b.dataset.t; setProbe(b.dataset.t); });
  const top=G?Object.entries(heat).sort((a,b)=>b[1]-a[1]).slice(0,12):[];
  $("mctop").innerHTML=top.length?top.map(([i,v])=>`<div class="mci pg" data-i="${i}" title="Score: read 3, cited 2, search hit 1, over all sessions"><b>${v}</b>${esc(G.nodes[i].t)}</div>`).join("")
    :`<div class="mcempty">Nothing consulted yet.</div>`;
  $("mctop").querySelectorAll(".mci").forEach(el=>el.onclick=()=>select(G.nodes[+el.dataset.i],true));
  paintLog(); paintTl(); legend(); shape();
}
function hotStep(k){ if(stepHot===k) return; stepHot=k; refocus();
  document.querySelectorAll("#mcsegs i,#mclog .mcs").forEach(el=>el.classList.toggle("hot",k!=null&&+el.dataset.k===k)); }
function paintLog(){
  const t=trailOf(), el=$("mclog");
  if(!t){ el.innerHTML=`<div class="mcempty">No session on the map. Select one to see its search.</div>`; return; }
  const t0=(t.steps.find(s=>s.at)||{}).at||0;
  el.innerHTML=t.steps.map((s,k)=>{ const x=stepText(s);
    const pills=s.tool==="search"?s._terms.map((q,i)=>`<u style="background:${TERMC[i%6]}">${esc(q)}</u>`).join(""):"";
    return `<div class="mcs ${s.error?"err":""} ${k>=stepMax?"fut":""}" data-k="${k}">
      <b>${String(k+1).padStart(2,"0")}</b><i class="${x.cls}">${esc(x.lbl)}</i><span>${esc(x.main)}${pills?`<br>${pills}`:""}${x.sub?`<small>${esc(x.sub)}</small>`:""}</span>
      <em>${s.at&&t0?tplus(s.at-t0):""}</em></div>`; }).join("");
  el.querySelectorAll(".mcs").forEach(r=>{ const k=+r.dataset.k;
    r.onmouseenter=()=>hotStep(k); r.onmouseleave=()=>hotStep(null);
    r.onclick=()=>{ const s=t.steps[k]; if(s._nodes.length) flyTo(nodesFor(k),110,Math.max(kfit*4.5,1.6)); if(s._focus) select(s._focus); }; });
}
function paintTl(){
  const t=trailOf(), el=$("mctl");
  if(!t){ el.innerHTML=`<div class="mcempty">Replay bar: select a Computer session to step through its search.</div>`; return; }
  const f=funnel(t);
  el.innerHTML=`<button class="pill ${play?"on":""}" id="mcplay" type="button" title="Play the session step by step">${play?"Pause":"Replay"}</button>
    <button class="pill" id="mcspeed" type="button" title="Replay speed">${speed}×</button>
    <button class="pill ${follow?"on":""}" id="mcfollow" type="button" title="The camera follows each step">Follow</button>
    <div id="mcsegs">${t.steps.map((s,k)=>{ const x=stepText(s); return `<i class="${x.cls} ${k>=stepMax?"fut":""}" data-k="${k}" title="${String(k+1).padStart(2,"0")} ${esc(x.lbl)}: ${esc(x.main)}"></i>`; }).join("")}</div>
    <button class="pill ${stepMax===Infinity?"on":""}" id="mcall" type="button" title="Show the whole session at once">All</button>
    <div id="mcfunnel"><span>Search<b>${f.search}</b></span>›<span>Hits<b>${f.hits}</b></span>›<span>Read<b>${f.read}</b></span>›<span>Cited<b>${f.cited}</b></span>${f.lock?`<span class="lock" title="Web searches refused because wiki content had been read">Web locked<b>${f.lock}</b></span>`:""}</div>`;
  $("mcplay").onclick=replay; $("mcall").onclick=showAll;
  $("mcspeed").onclick=()=>{ speed=speed===1?2:speed===2?.5:1; paintTl(); };
  $("mcfollow").onclick=()=>{ follow=!follow; paintTl(); };
  el.querySelectorAll("#mcsegs i").forEach(i=>{ const k=+i.dataset.k;
    i.onclick=()=>{ stopPlay(); stepHot=null; showStep(k); }; i.onmouseenter=()=>hotStep(k); i.onmouseleave=()=>hotStep(null); });
}

/* ---------------------------------------------------------------- view hooks (setView in fleet.html) */
window.wikiOpen=()=>{
  if(!built) build();
  lastAct=performance.now();
  if(resize()&&G&&!userCam) camTo=fitAll();
  loadGraph(); loadTrails(); clearInterval(timer);
  timer=setInterval(()=>{ if(view!=="wiki") return; loadTrails(); if(Date.now()/1000-gAt>600) loadGraph(); },15000);
  touch();
};
window.wikiShowChat=id=>{      /* Computer's "Access log" button: that conversation's trail, whole session shown */
  chatSel=id; LS.set("mcNoChat",false); stepHot=null; if(play) stopPlay();
  if(built&&trails.find(t=>t.id===id)){ showAll(); paintLists(); }   /* not loaded yet: loadTrails keeps chatSel when it finds it */
  const l=$("mclog"); if(l) l.scrollTop=0;
};
window.wikiClose=()=>{ clearInterval(timer); timer=null; if(play) stopPlay(); };
if(typeof view!=="undefined"&&view==="wiki") wikiOpen();
})();
