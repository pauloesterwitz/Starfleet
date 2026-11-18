/* ================= Memory Core =================
   The LLM wiki drawn as the ship's memory core: one ring per page type (other, concepts, entities, sources from the centre out),
   each page a radial tick whose length is its inbound link weight. Pages are ordered around the core so that linked pages sit
   close together; the dominant tag of each sector labels the rim. A Computer session that used the wiki is drawn on top as
   its course: search contacts (and the relation hops search followed), related fans, pages read (numbered) and pages cited.
   Data: /api/computer/wiki/graph (wiki-mcp's own link graph) and /api/computer/wiki/trails (Computer's chat files), both
   behind the Starfleet key like the rest of Computer. Read-only. Uses fleet.html's compJSON, esc, view and setView. */
(()=>{
const RINGS={other:["Other","#f96",.22],concept:["Concepts","#9cf",.45],entity:["Entities","#fc6",.67],source:["Sources","#c9c",.89]};
const ORDER=["other","concept","entity","source"];
const ONE={other:"Other",concept:"Concept",entity:"Entity",source:"Source"};
const tplus=s=>{ s=Math.max(0,Math.round(s)); return "T+"+(s<60?s+"s":Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s"); };
const KIND={ask:"Query",overview:"Scan",search:"Search",related:"Related",read:"Read",answer:"Answer",web:"Web"};
const C={course:"#f90",hit:"#f90",read:"#fff",readRing:"#9cf",cite:"#d46ab8",rel:"#c9c",dim:"#3a2c18"};
const $=id=>document.getElementById(id);
let G=null, gAt=0, byPath={}, byName={}, trails=[], chatSel=null, stepHot=null, stepMax=Infinity, sel=null, hov=null;
let placed=[], showLinks=false, showHeat=false, timer=null, playT=null, built=false, geo={cx:0,cy:0,R:1,dpr:1}, heat={}, sectors=[];

function build(){
  $("wikiview").innerHTML=`
    <div class="top"><div class="title">Memory <span>Core</span></div><div class="bar"><span id="mcmeta">--</span></div></div>
    <div class="vmsg" id="mcmsg"></div>
    <div id="mc">
      <div id="mcl"><div class="mch">Computer sessions</div><div id="mcchats"></div>
        <div class="mch">Most consulted</div><div id="mctop"></div></div>
      <div id="mcc"><div id="mctools"><input id="mcq" placeholder="Locate page" autocomplete="off" list="mcql"><datalist id="mcql"></datalist>
          <button class="pill" id="mclinks" type="button" title="Draw every link of the wiki">Links</button>
          <button class="pill" id="mcheat" type="button" title="Colour pages by how often Computer consulted them, over all sessions">Heat</button>
          <button class="pill" id="mcplay" type="button" title="Replay the selected session step by step">Replay</button>
          <span id="mclegend"></span></div>
        <div id="mcbox"><canvas id="mccv"></canvas><div id="mctip"></div></div></div>
      <div id="mcr"><div id="mcpage"></div><div class="mch">Access log</div><div id="mclog"></div></div>
    </div>`;
  $("mclegend").innerHTML=ORDER.map(k=>`<span><i style="background:${RINGS[k][1]}"></i>${RINGS[k][0]}</span>`).join("")+
    `<span><i style="background:${C.course}"></i>Course</span><span><i style="background:${C.cite}"></i>Cited</span>`;
  $("mclinks").onclick=()=>{ showLinks=!showLinks; $("mclinks").classList.toggle("on",showLinks); draw(); };
  $("mcheat").onclick=()=>{ showHeat=!showHeat; $("mcheat").classList.toggle("on",showHeat); draw(); };
  $("mcplay").onclick=replay;
  $("mcq").addEventListener("change",()=>{ const n=resolve($("mcq").value)||G&&G.nodes.find(n=>n.t.toLowerCase()===$("mcq").value.toLowerCase());
    if(n){ select(n); $("mcq").value=""; } });
  const cv=$("mccv");
  cv.addEventListener("mousemove",e=>{ const n=pick(e); if(n!==hov){ hov=n; draw(); } tip(e,n); });
  cv.addEventListener("mouseleave",()=>{ hov=null; tip(null,null); draw(); });
  cv.addEventListener("click",e=>select(pick(e)));
  addEventListener("resize",()=>{ if(view==="wiki") draw(); });
  built=true;
}
function msg(t){ const m=$("mcmsg"); m.textContent=t||""; m.style.display=t?"block":"none"; m.classList.toggle("err",!!t); }

/* ---- data ---- */
async function loadGraph(){
  try{
    const j=await compJSON("/api/computer/wiki/graph");
    if(j.error) msg(j.error); else msg("");
    if(!j.graph){ setTimeout(()=>{ if(view==="wiki") loadGraph(); },3000); return; }
    if(G&&j.at===gAt) return;
    gAt=j.at; setGraph(j.graph); mark(); paintLists(); draw();
  }catch(e){ msg(e.message); }
}
async function loadTrails(){
  try{
    const j=await compJSON("/api/computer/wiki/trails");
    const before=JSON.stringify(trails.map(t=>[t.id,t.steps.length]));
    trails=j.trails||[];
    if(before===JSON.stringify(trails.map(t=>[t.id,t.steps.length]))) return;
    if(!trails.find(t=>t.id===chatSel)) chatSel=trails[0]?trails[0].id:null;   /* newest session by default: a live answer grows */
    mark(); paintLists(); draw();
  }catch(e){ msg(e.message); }
}
function setGraph(g){
  byPath={}; byName={};
  const nodes=g.nodes.map(([p,t,type,tags,w,sum],i)=>({i,p,t,type,tags,w,sum,ring:RINGS[type]?type:"other",out:[],inn:[]}));
  nodes.forEach(n=>{ byPath[n.p]=n; byName[n.t.toLowerCase()]=byName[n.t.toLowerCase()]||n;
    const stem=n.p.split("/").pop().replace(/\.md$/,"").toLowerCase(); byName[stem]=byName[stem]||n; });
  g.edges.forEach(([u,v,k])=>{ nodes[u].out.push([nodes[v],k]); nodes[v].inn.push([nodes[u],k]); });
  G={nodes,edges:g.edges,stubs:g.stubs,log:g.log_mtime};
  layout(); sectorLabels();
  $("mcql").innerHTML=nodes.map(n=>`<option value="${esc(n.t)}">`).join("");
  const cnt=k=>nodes.filter(n=>n.ring===k).length;
  $("mcmeta").textContent=`${nodes.length} pages · ${g.edges.length} links · `+ORDER.map(k=>`${cnt(k)} ${RINGS[k][0]}`).join(" · ")+
    (g.log_mtime?` · last change ${ago(g.log_mtime)}`:"");
}
function resolve(s){
  if(!s||!G) return null;
  s=String(s).trim();
  return byPath[s]||byPath["wiki/"+s]||byName[s.toLowerCase()]||byName[s.split("/").pop().replace(/\.md$/i,"").toLowerCase()]||null;
}

/* ---- layout: rings by type, linked pages pulled together (circular barycentre, rings kept evenly filled) ---- */
function layout(){
  const N=G.nodes, adj=N.map(()=>[]), TAU=Math.PI*2;
  G.edges.forEach(([u,v,k])=>{ const w=k==="r"?1:.6; adj[u].push([v,w]); adj[v].push([u,w]); });
  const rings=ORDER.map(k=>N.filter(n=>n.ring===k));
  rings.forEach(r=>{ r.sort((a,b)=>((a.tags[1]||"~")+a.t).localeCompare((b.tags[1]||"~")+b.t)); r.forEach((n,k)=>n.a=TAU*k/r.length); });
  for(let it=0;it<24;it++){
    N.forEach(n=>{ let s=0,c=0; for(const [m,w] of adj[n.i]){ s+=w*Math.sin(N[m].a); c+=w*Math.cos(N[m].a); }
      const t=(s||c)?Math.atan2(s,c):n.a, d=Math.atan2(Math.sin(t-n.a),Math.cos(t-n.a)); n.t2=(n.a+.7*d+TAU)%TAU; });
    rings.forEach(r=>{ if(!r.length) return; r.sort((a,b)=>a.t2-b.t2);
      let s=0,c=0; r.forEach((n,k)=>{ const d=n.t2-TAU*k/r.length; s+=Math.sin(d); c+=Math.cos(d); });
      const ph=Math.atan2(s,c); r.forEach((n,k)=>n.a=(TAU*k/r.length+ph+TAU)%TAU); });
  }
}
function sectorLabels(){
  const S=18, cnt=Array.from({length:S},()=>({})), all={};
  G.nodes.forEach(n=>n.tags.forEach(t=>all[t]=(all[t]||0)+1));
  const skip=new Set(["concept","entity","source","meta","session","comparison","reference","question"]);
  G.nodes.forEach(n=>{ const s=Math.floor(n.a/(Math.PI*2)*S)%S;
    n.tags.forEach(t=>{ if(!skip.has(t)&&all[t]<G.nodes.length*.12) cnt[s][t]=(cnt[s][t]||0)+1; }); });
  sectors=cnt.map((c,s)=>{ const [t,v]=Object.entries(c).sort((a,b)=>b[1]-a[1])[0]||[];
    return v>=4?{a:(s+.5)/S*Math.PI*2,t}:null; });
  sectors=sectors.map((x,s)=>x&&sectors[(s+S-1)%S]&&sectors[(s+S-1)%S].t===x.t?null:x);
}

/* ---- what the selected session touched ---- */
let M={hit:new Set(),read:new Map(),cite:new Set(),rel:new Set()};
function trailOf(){ return trails.find(t=>t.id===chatSel)||null; }
function mark(){
  heat={};
  const bump=(n,v)=>{ if(n) heat[n.i]=(heat[n.i]||0)+v; };
  if(G) trails.forEach(t=>t.steps.forEach(s=>{
    if(s.tool==="read") bump(resolve(s.target),3);
    s.hits.forEach(h=>bump(resolve(h.p),1));
    (s.cites||[]).forEach(c=>bump(resolve(c),2)); }));
}
function visibleSteps(){ const t=trailOf(); return t?t.steps.map((s,k)=>({s,k})).filter(x=>x.k<stepMax):[]; }
function courseOf(steps){   /* focus page per step, in order: the read/related page, or a search's best catalogue hit */
  const pts=[];
  steps.forEach(({s,k})=>{
    let n=null;
    if(s.tool==="read"||s.tool==="related") n=resolve(s.target);
    else if(s.tool==="search"){ const h=s.hits.find(h=>!h.via&&!h.text)||s.hits[0]; n=h&&resolve(h.p); }
    if(n&&(!pts.length||pts[pts.length-1].n!==n)) pts.push({n,k,s});
  });
  return pts;
}

/* ---- drawing ---- */
function pos(n){ const r=RINGS[n.ring][2]*geo.R; return [geo.cx+r*Math.cos(n.a-Math.PI/2), geo.cy+r*Math.sin(n.a-Math.PI/2)]; }
function curve(ctx,a,b){ const [x1,y1]=pos(a),[x2,y2]=pos(b), mx=(x1+x2)/2, my=(y1+y2)/2;
  ctx.moveTo(x1,y1); ctx.quadraticCurveTo(mx+(geo.cx-mx)*.45,my+(geo.cy-my)*.45,x2,y2); }
function draw(){
  const cv=$("mccv"); if(!cv||!G||view!=="wiki") return;
  const box=$("mcbox"), dpr=devicePixelRatio||1, W=box.clientWidth, H=box.clientHeight;
  if(!W||!H) return;
  if(cv.width!==W*dpr||cv.height!==H*dpr){ cv.width=W*dpr; cv.height=H*dpr; }
  const ctx=cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H); placed=[];
  geo={cx:W/2,cy:H/2,R:Math.max(60,Math.min(W,H)/2-34),dpr};
  const {cx,cy,R}=geo, t=trailOf(), steps=visibleSteps(), only=stepHot!=null?steps.filter(x=>x.k===stepHot):steps;
  const active=new Set(); let any=false;
  only.forEach(({s})=>{ any=true; [s.target,...s.hits.map(h=>h.p),...(s.cites||[])].forEach(p=>{ const n=resolve(p); if(n) active.add(n); }); });
  if(sel){ active.add(sel); sel.out.concat(sel.inn).forEach(([n])=>active.add(n)); }
  /* core: rings as LCARS tracks, hub in the middle */
  ORDER.forEach(k=>{ ctx.beginPath(); ctx.arc(cx,cy,RINGS[k][2]*R,0,Math.PI*2); ctx.strokeStyle="#1a1409"; ctx.lineWidth=R*.085; ctx.stroke(); });
  const scan=only.some(({s})=>s.tool==="overview");
  ORDER.forEach(k=>{ ctx.beginPath(); ctx.arc(cx,cy,RINGS[k][2]*R+R*.047,0,Math.PI*2);
    ctx.strokeStyle=scan?"rgba(255,153,0,.55)":"#2a1f10"; ctx.lineWidth=scan?2:1; ctx.setLineDash(scan?[6,6]:[2,10]); ctx.stroke(); });
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(cx,cy,R*.1,0,Math.PI*2); ctx.fillStyle="#f90"; ctx.fill();
  ctx.beginPath(); ctx.arc(cx,cy,R*.065,0,Math.PI*2); ctx.fillStyle="#fc6"; ctx.fill();
  ctx.fillStyle="#000"; ctx.font=`700 ${Math.max(9,R*.03)}px Antonio,sans-serif`; ctx.textAlign="center"; ctx.textBaseline="middle";
  ctx.fillText("CORE",cx,cy);
  /* sector names on the rim */
  ctx.font=`600 ${Math.max(9,Math.min(13,R*.03))}px Antonio,sans-serif`; ctx.fillStyle="#a4855f";
  sectors.forEach(s=>{ if(!s) return; const a=s.a-Math.PI/2, r=R+18; ctx.save(); ctx.translate(cx+r*Math.cos(a),cy+r*Math.sin(a));
    let rot=a+Math.PI/2; if(rot>Math.PI/2&&rot<Math.PI*1.5) rot+=Math.PI; ctx.rotate(rot); ctx.fillText(s.t.toUpperCase(),0,0); ctx.restore(); });
  /* links */
  if(showLinks){ ctx.strokeStyle="rgba(153,204,255,.1)"; ctx.lineWidth=1; ctx.beginPath();
    G.edges.forEach(([u,v])=>curve(ctx,G.nodes[u],G.nodes[v])); ctx.stroke(); }
  const fan=(n,color,alpha)=>{ if(!n) return; ctx.strokeStyle=color; ctx.globalAlpha=alpha; ctx.lineWidth=1.2; ctx.beginPath();
    n.out.concat(n.inn).forEach(([m])=>curve(ctx,n,m)); ctx.stroke(); ctx.globalAlpha=1; };
  if(sel) fan(sel,RINGS[sel.ring][1],.55);
  if(hov&&hov!==sel) fan(hov,"#a4855f",.35);
  /* pages: radial ticks */
  const hmax=Math.max(1,...Object.values(heat));
  G.nodes.forEach(n=>{
    const r=RINGS[n.ring][2]*R, L=R*.022+Math.min(R*.05,R*.012*Math.log2(1+n.w)), a=n.a-Math.PI/2, ca=Math.cos(a), sa=Math.sin(a);
    let col=RINGS[n.ring][1], al=1;
    if(showHeat){ const h=heat[n.i]||0; col=h?`hsl(${30-28*h/hmax},100%,${45+20*h/hmax}%)`:C.dim; }
    else if((any||sel)&&!active.has(n)) al=.28;
    ctx.globalAlpha=al; ctx.strokeStyle=col; ctx.lineWidth=n===sel||n===hov?3:1.6;
    ctx.beginPath(); ctx.moveTo(cx+(r-L)*ca,cy+(r-L)*sa); ctx.lineTo(cx+(r+L)*ca,cy+(r+L)*sa); ctx.stroke(); });
  ctx.globalAlpha=1;
  /* the session's course */
  if(t){
    only.forEach(({s})=>{
      if(s.tool==="search") s.hits.forEach(h=>{ const n=resolve(h.p), v=h.via&&resolve(h.via);
        if(v&&n){ ctx.setLineDash([4,4]); ctx.strokeStyle=C.hit; ctx.globalAlpha=.7; ctx.lineWidth=1.2; ctx.beginPath(); curve(ctx,v,n); ctx.stroke();
          ctx.setLineDash([]); ctx.globalAlpha=1; }
        if(n){ const [x,y]=pos(n); ctx.beginPath(); ctx.arc(x,y,4.5,0,Math.PI*2); ctx.strokeStyle=C.hit; ctx.lineWidth=1.5; ctx.stroke(); } });
      if(s.tool==="related"){ const n=resolve(s.target); if(n){ ctx.strokeStyle=C.rel; ctx.lineWidth=1.4; ctx.globalAlpha=.8; ctx.beginPath();
        s.hits.forEach(h=>{ const m=resolve(h.p); if(m) curve(ctx,n,m); }); ctx.stroke(); ctx.globalAlpha=1; } }
    });
    const course=courseOf(steps);
    if(course.length>1){ ctx.strokeStyle=C.course; ctx.lineWidth=2.6; ctx.globalAlpha=stepHot!=null?.35:.95; ctx.beginPath();
      for(let i=1;i<course.length;i++) curve(ctx,course[i-1].n,course[i].n); ctx.stroke(); ctx.globalAlpha=1; }
    only.forEach(({s})=>(s.cites||[]).forEach(c=>{ const n=resolve(c); if(!n) return; const [x,y]=pos(n);
      ctx.beginPath(); ctx.arc(x,y,9,0,Math.PI*2); ctx.strokeStyle=C.cite; ctx.lineWidth=2.2; ctx.stroke(); }));
    let num=0;
    course.forEach(({n,k,s})=>{ if(s.tool==="read") num++; if(stepHot!=null&&k!==stepHot) return; const [x,y]=pos(n), read=s.tool==="read";
      ctx.beginPath(); ctx.arc(x,y,read?7:6,0,Math.PI*2); ctx.fillStyle=read?C.read:"#000"; ctx.fill();
      ctx.strokeStyle=read?C.readRing:C.course; ctx.lineWidth=2.4; ctx.stroke();
      ctx.font="700 10px Antonio,sans-serif"; ctx.fillStyle="#000"; ctx.textAlign="center"; ctx.textBaseline="middle";
      if(read) ctx.fillText(String(num),x,y+.5);
      label(ctx,n,x,y,read?C.readRing:C.course); });
  }
  if(sel){ const [x,y]=pos(sel); ctx.beginPath(); ctx.arc(x,y,8,0,Math.PI*2); ctx.strokeStyle="#fff"; ctx.lineWidth=2; ctx.stroke();
    label(ctx,sel,x,y,"#fff"); }
}
function label(ctx,n,x,y,col){   /* labels never overlap: a colliding one moves up or down, with a leader line to its page */
  if(placed.some(r=>r[4]===n)) return;   /* one label per page */
  const out=x>=geo.cx, s=n.t.length>34?n.t.slice(0,33)+"…":n.t;
  ctx.font="600 12px Antonio,sans-serif"; ctx.textAlign=out?"left":"right"; ctx.textBaseline="middle";
  const w=ctx.measureText(s).width, tx=x+(out?12:-12), x0=out?tx-3:tx-w-3, x1=x0+w+6;
  let dy=0;
  for(const d of [0,-17,17,-34,34,-51,51,-68,68]){ dy=d;
    if(!placed.some(r=>x0<r[2]&&x1>r[0]&&y+d-8<r[3]&&y+d+8>r[1])) break; }
  placed.push([x0,y+dy-8,x1,y+dy+8,n]);
  if(dy){ ctx.strokeStyle=col; ctx.globalAlpha=.6; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(out?x0:x1,y+dy);
    ctx.stroke(); ctx.globalAlpha=1; }
  ctx.fillStyle="rgba(0,0,0,.78)"; ctx.fillRect(x0,y+dy-8,w+6,16);
  ctx.fillStyle=col; ctx.fillText(s,tx,y+dy);
}
function pick(e){
  if(!G) return null;
  const b=$("mccv").getBoundingClientRect(), x=e.clientX-b.left, y=e.clientY-b.top;
  let best=null, d=11*11;
  G.nodes.forEach(n=>{ const [px,py]=pos(n), q=(px-x)**2+(py-y)**2; if(q<d){ d=q; best=n; } });
  return best;
}
function tip(e,n){
  const el=$("mctip"); if(!n||!e){ el.style.display="none"; return; }
  const b=$("mcbox").getBoundingClientRect();
  el.innerHTML=`${esc(n.t)}<small>${esc(ONE[n.ring])}${heat[n.i]?" · consulted":""}</small>`;
  el.style.display="block"; el.style.left=Math.min(e.clientX-b.left+14,b.width-el.offsetWidth-4)+"px"; el.style.top=(e.clientY-b.top+14)+"px";
}

/* ---- panels ---- */
function select(n){ sel=n; paintPage(); draw(); }
function paintPage(){
  const el=$("mcpage");
  if(!sel){ el.style.display="none"; return; }
  const n=sel, col=RINGS[n.ring][1], nb=[...n.out.map(([m,k])=>[m,k,"to"]),...n.inn.map(([m,k])=>[m,k,"from"])];
  const reads=trails.reduce((a,t)=>a+t.steps.filter(s=>s.tool==="read"&&resolve(s.target)===n).length,0);
  el.style.display="block"; el.style.borderLeftColor=col;
  el.innerHTML=`<h3 style="color:${col}">${esc(n.t)}<button type="button" id="mcpx">Close</button></h3>
    <div class="pth">${esc(n.type||"untyped")} · ${esc(n.p)}</div>${n.sum?`<p>${esc(n.sum)}</p>`:""}
    <div class="tg">${n.tags.map(t=>`<u>${esc(t)}</u>`).join("")}</div>
    <div class="kv"><span>Links out<b>${n.out.length}</b></span><span>In<b>${n.inn.length}</b></span><span>Weight<b>${n.w}</b></span><span>Read by Computer<b>${reads}</b></span></div>
    <div class="nb">${nb.slice(0,40).map(([m,k,d])=>`<a data-p="${esc(m.p)}">${esc(m.t)}<small>${d} · ${k==="r"?"related":k==="s"?"source":"link"}</small></a>`).join("")}
      ${nb.length>40?`<small class="mcempty">${nb.length-40} more</small>`:""}</div>`;
  $("mcpx").onclick=()=>select(null);
  el.querySelectorAll(".nb a").forEach(a=>a.onclick=()=>select(byPath[a.dataset.p]));
}
function paintLists(){
  if(!built) return;
  const sum=t=>{ const c=k=>t.steps.filter(s=>s.tool===k).length;
    return [c("read")&&`${c("read")} read`,c("search")&&`${c("search")} search`,c("related")&&`${c("related")} related`].filter(Boolean).join(" · "); };
  $("mcchats").innerHTML=trails.length?trails.map(t=>`<div class="mci ${t.id===chatSel?"on":""}" data-id="${esc(t.id)}">${esc(t.title)}
      <small>${sum(t)} · ${ago(t.updated)}</small></div>`).join("")
    :`<div class="mcempty">No Computer session has used the wiki yet. Ask Computer about your notes; its course through the core shows here.</div>`;
  $("mcchats").querySelectorAll(".mci").forEach(el=>el.onclick=()=>{ chatSel=el.dataset.id; stepMax=Infinity; stepHot=null; stopPlay(); paintLists(); draw(); });
  const top=G?Object.entries(heat).sort((a,b)=>b[1]-a[1]).slice(0,10):[];
  $("mctop").innerHTML=top.length?top.map(([i,v])=>`<div class="mci pg" data-i="${i}" title="Score: read 3, cited 2, search hit 1, over all sessions"><b>${v}</b>${esc(G.nodes[i].t)}</div>`).join("")
    :`<div class="mcempty">Nothing consulted yet.</div>`;
  $("mctop").querySelectorAll(".mci").forEach(el=>el.onclick=()=>select(G.nodes[+el.dataset.i]));
  paintLog();
}
function paintLog(){
  const t=trailOf(), el=$("mclog");
  if(!t){ el.innerHTML=`<div class="mcempty">Select a session.</div>`; return; }
  const t0=t.steps.find(s=>s.at)?.at||0;
  el.innerHTML=t.steps.map((s,k)=>{
    const n=resolve(s.target), via=s.hits.filter(h=>h.via).length, txt=s.hits.filter(h=>h.text).length;
    let main="", sub="";
    if(s.tool==="ask"){ main=s.arg; }
    else if(s.tool==="overview"){ main="Catalogue scan"; sub=s.pages?`${s.pages} pages`:""; }
    else if(s.tool==="search"){ main=`“${s.arg}”`; sub=`${s.hits.length} hits${via?` · ${via} via relations`:""}${txt?` · ${txt} full text`:""}`; }
    else if(s.tool==="read"||s.tool==="related"){ main=n?n.t:s.arg; sub=s.tool==="related"?`${s.hits.length} neighbours · depth ${s.depth||1}`:(n?ONE[n.ring]:"not in graph"); }
    else if(s.tool==="answer"){ const c=[...new Set((s.cites||[]).map(resolve).filter(Boolean))]; main=c.length?`Cites ${c.length} page${c.length>1?"s":""}`:"No page cited";
      sub=c.slice(0,4).map(n=>n.t).join(" · "); }
    else if(s.tool==="web"){ main=`${s.name}: ${s.arg}`; sub=s.blocked?"Privacy lock: blocked after a wiki read":"left the ship"; }
    const lbl=s.tool==="web"&&s.blocked?"Locked":KIND[s.tool]||s.tool;
    return `<div class="mcs k-${esc(s.tool)} ${s.blocked?"lock":""} ${s.error?"err":""} ${k>=stepMax?"fut":""} ${k===stepHot?"hot":""}" data-k="${k}">
      <b>${String(k+1).padStart(2,"0")}</b><i>${esc(lbl)}</i><span>${esc(main)}${sub?`<small>${esc(sub)}</small>`:""}</span>
      <em>${s.at&&t0?tplus(s.at-t0):""}</em></div>`; }).join("");
  el.querySelectorAll(".mcs").forEach(r=>{
    r.onmouseenter=()=>{ stepHot=+r.dataset.k; draw(); };
    r.onmouseleave=()=>{ stepHot=null; draw(); };
    r.onclick=()=>{ const s=t.steps[+r.dataset.k], n=resolve(s.target)||(s.hits[0]&&resolve(s.hits[0].p)); if(n) select(n); };
  });
}
function stopPlay(){ clearInterval(playT); playT=null; $("mcplay").classList.remove("on"); }
function replay(){
  const t=trailOf(); if(!t) return;
  if(playT){ stopPlay(); stepMax=Infinity; paintLog(); draw(); return; }
  stepMax=0; $("mcplay").classList.add("on");
  playT=setInterval(()=>{ stepMax++; paintLog(); draw(); if(stepMax>=t.steps.length){ stopPlay(); stepMax=Infinity; } },1100);
  paintLog(); draw();
}

/* ---- view hooks (setView in fleet.html) ---- */
window.wikiOpen=()=>{
  if(!built) build();
  loadGraph(); loadTrails(); clearInterval(timer);
  timer=setInterval(()=>{ if(view!=="wiki") return; loadTrails(); if(Date.now()/1000-gAt>600) loadGraph(); },15000);
  requestAnimationFrame(draw);
};
window.wikiClose=()=>{ clearInterval(timer); timer=null; if(playT) stopPlay(); };
if(typeof view!=="undefined"&&view==="wiki") wikiOpen();
})();
