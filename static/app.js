const $ = (s) => document.querySelector(s);
let conversationId = null;
let sending = false;
const safe = (v) => String(v ?? "");

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(body?.error || `Request failed (${response.status})`);
  return body;
}
const json = (value) => ({method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)});

async function refreshConversations(selectId = null) {
  const data = await api("/api/conversations");
  const select = $("#conversation-list");
  select.replaceChildren(new Option("Choose a conversation…", ""));
  for (const conv of data.conversations) {
    const opt = new Option(`${conv.title} · ${conv.domain}`, conv.id);
    select.add(opt);
  }
  if (selectId) select.value = String(selectId);
}
async function startConversation(clearCurrent = false) {
  if (clearCurrent && conversationId) await api(`/api/conversations/${conversationId}`, {method:"DELETE"});
  const created = await api("/api/conversations", json({domain:$("#domain").value}));
  conversationId = created.id;
  $("#messages").replaceChildren();
  $("#metrics").textContent = "No response metrics yet.";
  await refreshConversations(conversationId);
}
function makeMessage(role, content, details = {}) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const label = document.createElement("strong"); label.textContent = role === "assistant" ? "FlaskGPT" : "You";
  const text = document.createElement("div"); text.className = "message-text";
  if (role === "assistant" && details.sources?.length) {
    const refs = new Map(details.sources.map(src => [`${src.name} — paragraph ${src.paragraph}`, `source-${src.source_id}-${src.paragraph}`]));
    const pattern = /\[([^\]]+)\]/g; let from = 0; let match;
    while ((match = pattern.exec(content)) !== null) {
      text.append(document.createTextNode(content.slice(from, match.index)));
      const target = refs.get(match[1]);
      if (target) { const link = document.createElement("a"); link.href = `#${target}`; link.textContent = match[0]; link.onclick = () => { const d = link.closest("article").querySelector("details"); if (d) d.open = true; }; text.append(link); }
      else text.append(document.createTextNode(match[0]));
      from = pattern.lastIndex;
    }
    text.append(document.createTextNode(content.slice(from)));
  } else text.textContent = content;
  article.append(label, text);
  if (details.metrics) {
    const m = details.metrics;
    const meta = document.createElement("small");
    meta.textContent = `${m.response_ms} ms · ${m.input_tokens} input + ${m.output_tokens} output tokens${m.estimated ? " (estimated)" : ""}`;
    article.append(meta);
  }
  if (details.sources?.length) {
    const list = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = `Sources (${details.sources.length} excerpts)`; list.append(summary);
    for (const src of details.sources) { const p = document.createElement("p"); p.id = `source-${src.source_id}-${src.paragraph}`; p.textContent = `${src.name}, paragraph ${src.paragraph}: ${src.text}`; list.append(p); }
    article.append(list);
  }
  if (role === "assistant" && details.id) {
    const rating = document.createElement("div"); rating.className = "ratings"; rating.append(document.createTextNode("Rate: "));
    const applyRatingColor = (n) => { rating.classList.remove("rating-low","rating-mid","rating-high"); if(n) rating.classList.add(n<=2?"rating-low":n===3?"rating-mid":"rating-high"); [...rating.querySelectorAll("button")].forEach((x,i)=>x.classList.toggle("selected",i+1===n)); };
    for (let n=1;n<=5;n++) { const b=document.createElement("button"); b.type="button"; b.textContent=String(n); b.title=`Rate ${n} out of 5`; b.onclick=async()=>{try{await api(`/api/messages/${details.id}/rating`,json({rating:n})); applyRatingColor(n);}catch(e){setStatus(e.message,true);}}; rating.append(b); }
    applyRatingColor(details.rating);
    article.append(rating);
  }
  $("#messages").append(article); article.scrollIntoView({behavior:"smooth",block:"end"});
  return article;
}
async function loadConversation(id) {
  if (!id) return;
  const data = await api(`/api/conversations/${id}`);
  conversationId = data.id; $("#domain").value = data.domain; $("#messages").replaceChildren();
  for (const msg of data.messages) makeMessage(msg.role,msg.content,{id:msg.id,rating:msg.rating,metrics:msg.role==="assistant"&&msg.response_ms!=null?{response_ms:msg.response_ms,input_tokens:msg.input_tokens,output_tokens:msg.output_tokens,estimated:!!msg.token_estimated}:null,sources:msg.sources});
  await refreshMetrics();
}
async function refreshMetrics() {
  if (!conversationId) return;
  const m = await api(`/api/conversations/${conversationId}/metrics`);
  $("#metrics").textContent = `${m.requests} requests · avg ${Number(m.avg_ms).toFixed(0)} ms · ${m.input_tokens} input + ${m.output_tokens} output tokens${m.estimated ? " (estimates)" : ""}`;
}
function setStatus(text, error=false) { const s=$("#status"); s.textContent=text; s.className=error?"error":""; }
async function loadSources() {
  const {sources} = await api("/api/sources"); const box=$("#source-list"); box.replaceChildren();
  for (const src of sources) { const label=document.createElement("label"); label.className="source-choice"; const cb=document.createElement("input"); cb.type="checkbox"; cb.value=src.id; const span=document.createElement("span"); span.textContent=src.name; const del=document.createElement("button"); del.type="button"; del.textContent="×"; del.title="Delete source"; del.onclick=async()=>{await api(`/api/sources/${src.id}`,{method:"DELETE"});await loadSources();}; label.append(cb,span,del); box.append(label); }
}
$("#new-chat").addEventListener("click", async()=>{try{await startConversation(true);setStatus("Started a new conversation; the previous one was deleted.");}catch(e){setStatus(e.message,true);}});
$("#conversation-list").addEventListener("change", async(e)=>{try{await loadConversation(e.target.value);}catch(err){setStatus(err.message,true);}});
$("#prompt-form").addEventListener("submit", async(e)=>{
  e.preventDefault(); if(sending)return; const question=$("#prompt").value.trim(); if(!question)return;
  sending=true; $("#send").disabled=true; setStatus("Thinking…"); makeMessage("user",question); $("#prompt").value="";
  try {
    if(!conversationId) await startConversation(false);
    const sourceIds=[...document.querySelectorAll("#source-list input:checked")].map(x=>Number(x.value));
    const result=await api("/prompt",json({conversation_id:conversationId,domain:$("#domain").value,messages:[question],source_ids:sourceIds,excerpt_count:Number($("#excerpt-count").value)||3}));
    makeMessage("assistant",result.reply,{id:result.message_id,metrics:result.metrics,sources:result.sources});
    const m=result.metrics; $("#metrics").textContent=`Latest ${m.response_ms} ms · ${m.input_tokens} input + ${m.output_tokens} output tokens${m.estimated?" (estimated)":""}`;
    await refreshConversations(conversationId); setStatus("");
  } catch(err) { makeMessage("assistant",`Error: ${err.message}`); setStatus(err.message,true); }
  finally {sending=false;$("#send").disabled=false;}
});
$("#source-form").addEventListener("submit",async(e)=>{e.preventDefault();try{await api("/api/sources",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({name:$("#source-name").value,content:$("#source-text").value})});e.target.reset();await loadSources();setStatus("Source added.");}catch(err){setStatus(err.message,true);}});
document.querySelectorAll("[data-export]").forEach(b=>b.onclick=()=>{if(!conversationId){setStatus("Start or select a conversation first.",true);return;}location.href=`/api/conversations/${conversationId}/export/${b.dataset.export}`;});
$("#summary-pdf").onclick=async()=>{if(!conversationId){setStatus("Start or select a conversation first.",true);return;}try{const r=await fetch(`/api/conversations/${conversationId}/summary.pdf`,{method:"POST"});if(!r.ok){const e=await r.json();throw new Error(e.error);}const a=document.createElement("a");a.href=URL.createObjectURL(await r.blob());a.download=`conversation-${conversationId}-summary.pdf`;a.click();URL.revokeObjectURL(a.href);}catch(e){setStatus(e.message,true);}};
$("#data-form").addEventListener("submit",async(e)=>{e.preventDefault();const fd=new FormData();fd.append("file",$("#data-file").files[0]);setStatus("Analyzing dataset…");try{const result=await api("/api/data/analyze",{method:"POST",body:fd});$("#data-report").textContent=JSON.stringify(result.report,null,2);setStatus("Analysis complete; report saved.");}catch(err){setStatus(err.message,true);}});
(async()=>{try{await refreshConversations();await loadSources();await startConversation(false);}catch(e){setStatus(e.message,true);}})();
