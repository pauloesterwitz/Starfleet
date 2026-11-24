/* ================= Starfleet PADD: OpenCode sessions =================
   The same component as the desktop's Computer > Sessions (vendor/ocsess.js). Two screens so Back works:
   #/computer/sessions            the list
   #/computer/session/<node>/<id> one session
   Opened from the Sessions button in Computer's header. */
(() => {
"use strict";
const P = window.PADD;
const api = { get: u => P.api.get(u), post: (u, b) => P.api.post(u, b) };
let inst = null;
const open = (el, opts) => { inst = OC.mount(el, Object.assign({api, compact: true}, opts)); };
P.screens["computer/sessions"] = { tab: "computer", title: "Sessions", color: "magenta", parent: "#/computer", stage: true,
  mount(el){ P.mdReady && P.mdReady().catch(() => {});
    open(el, { onSelect: s => { if(s) P.go(`#/computer/session/${s.node}/${encodeURIComponent(s.id)}`); } }); },
  unmount(){ if(inst){ inst.unmount(); inst = null; } } };
P.screens["computer/session"] = { tab: "computer", title: "Session", color: "magenta", parent: "#/computer/sessions", stage: true,
  mount(el, arg){ P.mdReady && P.mdReady().catch(() => {}); const [node, id] = String(arg || "").split("/");
    open(el, { node, id, onSelect: s => { if(!s) P.go("#/computer/sessions", true); } }); },
  unmount(){ if(inst){ inst.unmount(); inst = null; } } };
})();
