/* Speech family of Shuttle Bay: the speech-to-text servers (Whisper, Cohere Transcribe, Voxtral) that Fleet starts and stops
   itself through speech-ctl, one row per member and machine. They are not llama-swap members, so /api/state carries them in
   their own "speech" object (speech.py); this file turns that into rows for the Speech filter and sends the keyed Load and
   Unload requests. Loaded BEFORE the main script on purpose: isSpeech must exist when render() first runs. Everything else
   (busy, last, render, refresh, toast, queuedPos) is looked up at call time in the main script. */
const isSpeech=m=>m.kind==="speech";
const spEsc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const spGB=mb=>mb==null?"--":(mb/1024).toFixed(1)+" GB";
const SP_NODE={"jean-luc":"JEAN-LUC","kathryn":"KATHRYN"};

/* state.speech -> one pseudo model row per member and node. The row id carries the node suffix like every other row, so the
   state, queue and busy lookups stay per machine. RAM is what the server holds while it runs (measured by speech-ctl), else
   what it needs to start. */
function speechModels(sp){
  const rows=[];
  for(const m of (sp&&sp.members)||[]){
    for(const [node,p] of Object.entries(m.per_node||{})){
      const held=p.state==="ready"&&p.mem_mb!=null;
      rows.push(Object.assign({},m,p,{id:`${m.id}-${node}`,mid:m.id,node,nodes:[node],kind:"speech",pinned:!!p.pinned,tps:null,ctx:null,effort:null,effort_options:[],
        ram_mb:{[node]:held?p.mem_mb:m.need_mb},ram_basis:held?"measured: what the running server holds":"what the server needs to start (weights, cache and working memory)"}));
    }
  }
  return rows;
}
/* One row per MODEL, the machine picked in its Node dropdown (the same picker the LLM families use: machineSel + setMachine in the
   main script). Default pick: the machine it already runs on, else the emptier machine it fits on. The other machine's state
   stays visible on the row (_others), so a server running on the machine you did not pick is never hidden.
   Idempotent: render(last) runs again on every filter click, so rows from an earlier pass are dropped first. */
function withSpeech(s){
  if(!s||!s.speech) return s;
  const sel=typeof machineSel!=="undefined"?machineSel:{}, order=typeof MACHINE_ORDER!=="undefined"?MACHINE_ORDER:["jean-luc","kathryn"];
  const avail=n=>(((s.speech.nodes||{})[n])||{}).avail_mb||0, by={};
  speechModels(s.speech).forEach(r=>(by[r.mid]=by[r.mid]||[]).push(r));
  const rows=Object.values(by).map(list=>{
    const bn={}; list.forEach(r=>bn[r.node]=r);
    const busyish=r=>["ready","loading","queued","unloading"].includes(r.state);
    const live=list.find(busyish);
    const fits=list.filter(r=>(r.fit||{}).ok).sort((a,b)=>avail(b.node)-avail(a.node))[0];
    /* a tab that lists by state (Resident, Pinned) or by machine shows the machine that satisfies it, like the LLM families do */
    const flt=typeof filter!=="undefined"?filter:"";
    const forced=flt==="live"?list.find(r=>r.state==="ready"):flt==="pinned"?list.find(r=>r.pinned):bn[flt];
    const pick=forced||bn[sel[list[0].mid]]||list.find(r=>r.pinned)||live||fits||list.slice().sort((a,b)=>order.indexOf(a.node)-order.indexOf(b.node))[0];
    if(!forced||bn[flt]) sel[pick.mid]=pick.node;
    return Object.assign({},pick,{_base:pick.mid,_family:order.filter(n=>bn[n]),
      _others:list.filter(r=>r!==pick&&(busyish(r)||r.pinned)).map(r=>({node:r.node,state:r.state,pinned:r.pinned}))});
  });
  return Object.assign({},s,{models:(s.models||[]).filter(m=>!isSpeech(m)).concat(rows)});
}

function speechRow(m, working){
  const st=m.state, busyNow=working||st==="loading"||st==="unloading";
  const cls=st==="ready"?"ready":st==="loading"?"loading":st==="unloading"?"working":st==="queued"?"queued":"stopped";
  const qp=(typeof queuedPos!=="undefined"&&queuedPos[m.id])||0;
  const label=st==="queued"?"queued"+(qp?" #"+qp:""):st==="loading"?"loading"+(m.busy_s!=null?" "+m.busy_s+" s":""):st==="unloading"?"unloading":st==="ready"?"loaded":st==="unknown"?"unknown":"stopped";
  const fit=m.fit||{};
  const sub=[m.family||m.engine, m.precision, m.params_b?m.params_b+"B":null, m.languages, m.max_audio&&("audio: "+m.max_audio)].filter(Boolean).join(" · ");
  const others=(m._others||[]).map(o=>`${SP_NODE[o.node]||spEsc(o.node)}: ${o.state==="ready"?"loaded":o.state==="off"||o.state==="unknown"?"pinned":o.state}${o.pinned&&o.state!=="off"&&o.state!=="unknown"?", pinned":""}`).join(", ");
  const also=others?`<div class="sub">also on the other machine - ${spEsc(others)}</div>`:"";
  const where=st==="ready"&&m.url?`<div class="sub">${spEsc(m.url+(m.endpoint||""))}</div>`:"";
  const warn=(st==="off"||st==="unknown")&&fit.ok===false?`<div class="err">${spEsc(fit.reason)}</div>`:"";
  const lu=m.last&&m.last.op==="unload"&&m.last.ok&&st==="off"
    ?`<div class="sub">last unload freed ${spGB(m.last.freed_mb)}${m.last.cache_dropped_mb?` + ${spGB(m.last.cache_dropped_mb)} of weights dropped from cache`:""}</div>`:"";
  return `<tr class="${m.pinned?"pinned":st==="ready"?"live":""}">
    <td><div class="mid">${spEsc(m.label||m.mid)}</div>
        <div class="sub">${spEsc(sub)} </div>${where}${also}${typeof pinNote==="function"?pinNote(m,st==="ready"):""}${warn}${lu}${m.error?`<div class="err">${spEsc(m.error)}</div>`:""}</td>
    <td class="ctx"><select class="ctxsel set" title="Where this server runs. State, Load and Unload apply to the machine picked here; the same server can also run on the other machine."
        onchange="setMachine('${m._base}',this.value)">${(m._family||[m.node]).map(n=>
        `<option value="${n}"${n===m.node?" selected":""}>${SP_NODE[n]||spEsc(n)}</option>`).join("")}</select></td>
    <td>${ramCell(m)}</td>
    <td class="mid">&mdash;</td><td class="mid">&mdash;</td><td class="mid">&mdash;</td>
    <td><span class="st ${cls}">${label}</span></td>
    <td class="mid">&mdash;</td>
    <td class="act">${loadPinButtons({id:m.id,up:st==="ready",loading:st==="loading",queued:st==="queued",unloading:st==="unloading",pinned:m.pinned,working,
      blocked:fit.ok===false?fit.reason:"",load:`loadSpeech('${m.id}')`,unload:`unloadSpeech('${m.id}')`,pin:`pinSpeech('${m.id}',${!m.pinned})`})}</td></tr>`;
}

async function spPost(url,id,body){
  busy.add(id); render(last);
  try{
    const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(!r.ok) alert(j.error||"failed");
    return j;
  }catch(e){ alert(String(e)); }
  finally{ busy.delete(id); refresh(); }
}
const spRow=id=>(last.models||[]).find(m=>m.id===id);
async function loadSpeech(id){ const m=spRow(id); if(m) await spPost("/api/speech/load",id,{id:m.mid,node:m.node}); }
async function pinSpeech(id,on){ const m=spRow(id); if(m) await spPost("/api/speech/pin",id,{id:m.mid,node:m.node,pin:on}); }
async function unloadSpeech(id){
  const m=spRow(id); if(!m) return;
  const j=await spPost("/api/speech/unload",id,{id:m.mid,node:m.node});
  if(j&&j.ok&&!j.dequeued)
    toast(`${m.label||m.mid} unloaded on ${SP_NODE[m.node]||m.node}: ${spGB(j.freed_mb)} RAM freed`
      +(j.cache_dropped_mb?`, ${spGB(j.cache_dropped_mb)} of weights dropped from the cache`:"")
      +((j.leftovers_alive||[]).length?` - WARNING: ${j.leftovers_alive.length} process(es) still hold it`:""));
}

/* checks, in the style of the main page's console.asserts */
(function(){
  const sp={members:[{id:"a",label:"A",per_node:{"jean-luc":{state:"off"},kathryn:{state:"ready"}}}]};
  const s=withSpeech({models:[{id:"llm"}],speech:sp});
  console.assert(speechModels(sp)[1].id==="a-kathryn"&&speechModels(sp)[1].nodes[0]==="kathryn","speech: row id carries the node suffix");
  console.assert(s.models.length===2&&s.models.filter(isSpeech).length===1,"speech: ONE row per member (the machine is a dropdown), the LLM row stays");
  console.assert(s.models[1].node==="kathryn"&&s.models[1]._family.join()==="jean-luc,kathryn","speech: the row shows the machine it runs on, offers both");
  console.assert(withSpeech(s).models.length===2,"speech: withSpeech is idempotent (render(last) runs it again)");
  console.assert(withSpeech({speech:{members:[{id:"b",need_mb:5000,per_node:{"jean-luc":{state:"ready",mem_mb:4000},kathryn:{state:"off"}}}]}}).models[0].ram_mb["jean-luc"]===4000,
    "speech: a running server reports the memory it holds, not its estimate");
})();
