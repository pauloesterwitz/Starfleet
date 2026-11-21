/* Speech family of Shuttle Bay: the speech-to-text servers (Whisper, Cohere Transcribe, Voxtral) that Fleet starts and stops
   itself through speech-ctl, one row per member and machine. They are not llama-swap members, so /api/state carries them in
   their own "speech" object (speech.py); this file turns that into rows for the Speech filter and sends the keyed Load and
   Unload requests. Loaded BEFORE the main script on purpose: isSpeech must exist when render() first runs. Everything else
   (busy, last, render, refresh, toast, queuedPos) is looked up at call time in the main script. */
const isSpeech=m=>m.kind==="speech";
const spEsc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const spGB=mb=>mb==null?"--":(mb/1024).toFixed(1)+" GB";
const SP_NODE={"jean-luc":"JEAN-LUC","kathryn":"KATHRYN"};

/* state.speech -> one pseudo model row per member and node. The row id carries the node suffix like every other row. */
function speechModels(sp){
  const rows=[];
  for(const m of (sp&&sp.members)||[]){
    for(const [node,p] of Object.entries(m.per_node||{})){
      rows.push(Object.assign({},m,p,{id:`${m.id}-${node}`,mid:m.id,node,nodes:[node],kind:"speech",pinned:!!p.pinned,tps:null,ctx:null,effort:null,effort_options:[]}));
    }
  }
  return rows;
}
/* idempotent: render(last) runs again on every filter click, so rows from an earlier pass are dropped first */
function withSpeech(s){
  return s&&s.speech?Object.assign({},s,{models:(s.models||[]).filter(m=>!isSpeech(m)).concat(speechModels(s.speech))}):s;
}

function speechRow(m, working){
  const st=m.state, busyNow=working||st==="loading"||st==="unloading";
  const cls=st==="ready"?"ready":st==="loading"?"loading":st==="unloading"?"working":st==="queued"?"queued":"stopped";
  const qp=(typeof queuedPos!=="undefined"&&queuedPos[m.id])||0;
  const label=st==="queued"?"queued"+(qp?" #"+qp:""):st==="loading"?"loading"+(m.busy_s!=null?" "+m.busy_s+" s":""):st==="unloading"?"unloading":st==="ready"?"loaded":st==="unknown"?"unknown":"stopped";
  const fit=m.fit||{};
  const sub=[m.family||m.engine, m.precision, m.params_b?m.params_b+"B":null, m.languages, m.max_audio&&("audio: "+m.max_audio)].filter(Boolean).join(" · ");
  const held=st==="ready"&&m.mem_mb!=null?` &middot; holds ${spGB(m.mem_mb)}`:"";
  const where=st==="ready"&&m.url?`<div class="sub">${spEsc(m.url+(m.endpoint||""))}</div>`:"";
  const warn=(st==="off"||st==="unknown")&&fit.ok===false?`<div class="err">${spEsc(fit.reason)}</div>`:"";
  const lu=m.last&&m.last.op==="unload"&&m.last.ok&&st==="off"
    ?`<div class="sub">last unload freed ${spGB(m.last.freed_mb)}${m.last.cache_dropped_mb?` + ${spGB(m.last.cache_dropped_mb)} of weights dropped from cache`:""}</div>`:"";
  const can=!(busyNow||st==="ready"||st==="queued"||fit.ok===false);
  return `<tr class="${m.pinned?"pinned":st==="ready"?"live":""}">
    <td><div class="mid">${spEsc(m.label||m.mid)}</div>
        <div class="sub">${spEsc(sub)} &middot; needs ${spGB(m.need_mb)}${held}</div>${where}${warn}${lu}${m.error?`<div class="err">${spEsc(m.error)}</div>`:""}</td>
    <td class="ctx"><select class="ctxsel" disabled title="Each speech server runs on the machine of its row"><option>${SP_NODE[m.node]||spEsc(m.node)}</option></select></td>
    <td class="mid">&mdash;</td><td class="mid">&mdash;</td><td class="mid">&mdash;</td>
    <td><span class="st ${cls}">${label}</span></td>
    <td class="mid">&mdash;</td>
    <td class="act">
      <button class="${m.pinned?"on":""}" ${busyNow?"disabled":""} title="Keep this server loaded: Pin loads it now, Unload unpins it" onclick="pinSpeech('${m.id}',${!m.pinned})">${m.pinned?"Pinned":"Pin"}</button>
      <button class="${st==="ready"?"on":""}" ${can?"":"disabled"} title="${fit.ok===false&&st==="off"?spEsc(fit.reason):"Load this server on "+(SP_NODE[m.node]||spEsc(m.node))}"
        onclick="loadSpeech('${m.id}')">${st==="ready"?"Loaded":"Load"}</button>
      <button class="unl" ${(st==="off"||st==="unknown"||st==="unloading")?"disabled":""}
        title="${st==="ready"?"Stop the server and give its memory back":st==="queued"?"Drop it from the load queue":"Stop it"}"
        onclick="unloadSpeech('${m.id}')">${st==="queued"?"Dequeue":st==="loading"?"Cancel":"Unload"}</button></td></tr>`;
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
  console.assert(s.models.length===3&&s.models.filter(isSpeech).length===2,"speech: one row per member and node, the LLM row stays");
  console.assert(withSpeech(s).models.length===3,"speech: withSpeech is idempotent (render(last) runs it again)");
  console.assert(speechModels(sp)[1].id==="a-kathryn"&&speechModels(sp)[1].nodes[0]==="kathryn","speech: row id carries the node suffix");
})();
