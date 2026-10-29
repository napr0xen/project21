const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let currentChat = null, chats = [], activeView = "chat", user = null, busy = false;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// Small safe markdown renderer: escapes HTML first, then supports common Markdown.
function md(s) {
  let t = esc(s);
  const code = [];
  t = t.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_, lang, body) => { code.push(`<pre><code>${body.trim()}</code></pre>`); return `@@CODE${code.length-1}@@`; });
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/^### (.+)$/gm, "<h3>$1</h3>").replace(/^## (.+)$/gm, "<h2>$1</h2>").replace(/^# (.+)$/gm, "<h1>$1</h1>");
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\*(.+?)\*/g, "<em>$1</em>");
  t = t.replace(/^\s*[-*] (.+)$/gm, "<li>$1</li>").replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`);
  t = t.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
  t = t.split(/\n{2,}/).map(p => /^(<h\d|<ul>|<pre>|<blockquote>|@@CODE)/.test(p) ? p : `<p>${p.replace(/\n/g,"<br>")}</p>`).join("");
  t = t.replace(/@@CODE(\d+)@@/g, (_, i) => code[+i]);
  return t;
}
async function api(url, options={}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    ...(options.body instanceof FormData ? {} : {"Content-Type":"application/json"}),
    ...(options.headers || {})
  };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (token) headers["X-CSRF-Token"] = token;
  }
  const res = await fetch(url, {
    credentials:"same-origin",
    ...options,
    headers
  });
  if (res.status === 401) { location.href="/login"; throw new Error("Please sign in again"); }
  const body = await res.json().catch(()=>({}));
  if (!res.ok) throw new Error(body.detail || "Something went wrong");
  return body;
}
function toast(message) { let el=$("#toast"); if(!el){el=document.createElement("div");el.id="toast";el.style.cssText="position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#242421;color:white;padding:11px 16px;border-radius:9px;font-size:12px;z-index:20;box-shadow:0 8px 25px #0002";document.body.append(el);} el.textContent=message;el.style.display="block";clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.style.display="none",2800); }
function showView(view) {
  activeView=view;
  ["chat","documents","notes","memories","settings"].forEach(v=>$("#"+v+"View").classList.toggle("hidden",v!==view));
  $("#viewLabel").textContent=({chat:"Chat",documents:"Documents",notes:"Notes",memories:"Saved memories",settings:"Settings"})[view];
  $$(".nav-item").forEach(b=>b.classList.toggle("active",b.dataset.view===view));
  $("#sidebar").classList.remove("open");$("#scrim").classList.remove("show");
  if(view==="documents") loadDocuments(); if(view==="notes") loadNotes(); if(view==="memories") loadMemories(); if(view==="settings") loadSettings();
}
function renderChats(){
  $("#chatList").innerHTML=chats.map(c=>`<button class="history-item ${c.id===currentChat?'active':''}" data-chat="${c.id}" title="${esc(c.title)}">${esc(c.title)}</button>`).join("") || `<div class="tiny muted" style="padding:10px">Your conversations will appear here.</div>`;
  $$(".history-item").forEach(b=>b.onclick=()=>openChat(+b.dataset.chat));
}
async function loadChats(){chats=await api("/api/chats");renderChats();}
function addMessage(role, content) {
  $("#welcome").classList.add("hidden");
  const el=document.createElement("article");el.className="message "+role;
  el.innerHTML=`<div class="message-avatar">${role==="user"?"●":"✳"}</div><div class="message-body"><div class="message-name">${role==="user"?"You":"Assistant"}</div><div class="message-content">${md(content)}</div></div>`;
  $("#messages").append(el);window.scrollTo({top:document.body.scrollHeight,behavior:"smooth"});return el;
}
async function newChat() {
  const provider=$("#providerSelect").value, model=$("#modelInput").value.trim() || "qwen3.5:9b";
  const c=await api("/api/chats",{method:"POST",body:JSON.stringify({provider,model})});
  currentChat=c.id;$("#messages").innerHTML="";$("#welcome").classList.remove("hidden");await loadChats();showView("chat");$("#messageInput").focus();
}
async function openChat(id) {
  const data=await api(`/api/chats/${id}`);currentChat=id;$("#messages").innerHTML="";
  $("#providerSelect").value=data.chat.provider||"ollama";$("#modelInput").value=data.chat.model||"qwen3.5:9b";
  $("#welcome").classList.toggle("hidden",data.messages.length>0);
  data.messages.forEach(m=>addMessage(m.role,m.content));renderChats();showView("chat");
}
async function send(text) {
  if(busy||!text.trim())return;
  if(!currentChat) await newChat();
  showView("chat");const content=text.trim();$("#messageInput").value="";resizeInput();
  addMessage("user",content);const waiting=addMessage("assistant","Thinking…");waiting.querySelector(".message-content").classList.add("typing");
  busy=true;$("#sendBtn").disabled=true;
  try{
    const answer=await api(`/api/chats/${currentChat}/send`,{method:"POST",body:JSON.stringify({content,provider:$("#providerSelect").value,model:$("#modelInput").value.trim()})});
    waiting.querySelector(".message-content").classList.remove("typing");waiting.querySelector(".message-content").innerHTML=md(answer.content);
    await loadChats();
  }catch(e){waiting.querySelector(".message-content").textContent=e.message;toast(e.message);}
  finally{busy=false;$("#sendBtn").disabled=false;$("#messageInput").focus();}
}
async function loadSettings(){
  const s=await api("/api/settings");$("#settingsProvider").value=s.provider;$("#settingsModel").value=s.model;$("#settingsBase").value=s.api_base;$("#settingsKey").value="";
  $("#providerSelect").value=s.provider;$("#modelInput").value=s.model;
  try{const users=await api("/api/admin/users");$("#adminCard").classList.remove("hidden");$("#usersList").innerHTML=users.map(u=>`<div class="resource-card"><div class="resource-icon">♙</div><div class="resource-main"><strong>${esc(u.username)} ${u.id===user.id?"(you)":""}</strong><small>${esc(u.role)} · ${u.active?"Active":"Disabled"}</small></div>${u.id===user.id?"":`<button class="small-btn" data-toggle-user="${u.id}" data-active="${u.active?0:1}">${u.active?"Disable":"Enable"}</button>`}</div>`).join("");$$("[data-toggle-user]").forEach(b=>b.onclick=async()=>{try{await api(`/api/admin/users/${b.dataset.toggleUser}`,{method:"PATCH",body:JSON.stringify({active:b.dataset.active==="1"})});loadSettings();}catch(e){toast(e.message)}});}
  catch(e){$("#adminCard").classList.add("hidden");}
}
async function saveSettings(){
  const body={provider:$("#settingsProvider").value,model:$("#settingsModel").value.trim(),api_base:$("#settingsBase").value.trim(),api_key:$("#settingsKey").value||null};
  try{await api("/api/settings",{method:"PUT",body:JSON.stringify(body)});$("#settingsStatus").textContent="Settings saved.";$("#providerSelect").value=body.provider;$("#modelInput").value=body.model;$("#settingsKey").value="";toast("Settings saved");}
  catch(e){$("#settingsStatus").textContent=e.message;}
}
async function loadDocuments(){
  const docs=await api("/api/documents");$("#docCount").textContent=docs.length;
  $("#documentsList").innerHTML=docs.map(d=>`<div class="resource-card"><div class="resource-icon">▤</div><div class="resource-main"><strong>${esc(d.filename)}</strong><small>Added ${new Date(d.created_at).toLocaleDateString()}</small></div><button class="small-btn danger" data-delete-doc="${d.id}">Delete</button></div>`).join("")||`<p class="muted tiny">No documents yet. Upload a text file to get started.</p>`;
  $$("[data-delete-doc]").forEach(b=>b.onclick=async()=>{if(confirm("Delete this document?")){await api(`/api/documents/${b.dataset.deleteDoc}`,{method:"DELETE"});loadDocuments();}});
}
async function uploadFile(file){
  if(!file)return;const fd=new FormData();fd.append("file",file);
  try{await api("/api/documents",{method:"POST",body:fd});toast("Document uploaded");loadDocuments();$("#fileInput").value="";}
  catch(e){toast(e.message);}
}
async function searchDocuments(){
  const q=$("#searchInput").value.trim();if(!q)return;
  try{const results=await api(`/api/search?q=${encodeURIComponent(q)}`);
    $("#searchResults").innerHTML=results.map(r=>`<div class="resource-card"><div class="resource-icon">⌕</div><div class="resource-main"><strong>${esc(r.filename)} · ${r.score} matches</strong><p>${esc(r.excerpt)}</p></div></div>`).join("")||`<p class="muted tiny">No matching text found.</p>`;
  }catch(e){toast(e.message)}
}
async function loadNotes(){
 const notes=await api("/api/notes");
 $("#notesList").innerHTML=notes.map(n=>`<div class="resource-card"><div class="resource-icon">▧</div><div class="resource-main"><strong>${esc(n.title)}</strong><p>${esc(n.content.slice(0,500))}</p><small>Updated ${new Date(n.updated_at).toLocaleString()}</small></div><div class="resource-actions"><button class="small-btn" data-edit-note="${n.id}">Edit</button><button class="small-btn danger" data-delete-note="${n.id}">Delete</button></div></div>`).join("")||`<p class="muted tiny">Your notes will appear here.</p>`;
 $$("[data-edit-note]").forEach(b=>b.onclick=async()=>{const n=notes.find(x=>x.id===+b.dataset.editNote);$("#noteTitle").value=n.title;$("#noteContent").value=n.content;$("#saveNote").dataset.id=n.id;window.scrollTo({top:0,behavior:"smooth"});});
 $$("[data-delete-note]").forEach(b=>b.onclick=async()=>{if(confirm("Delete this note?")){await api(`/api/notes/${b.dataset.deleteNote}`,{method:"DELETE"});loadNotes();}});
}
async function loadMemories(){
 const memories=await api("/api/memories");
 $("#memoriesList").innerHTML=memories.map(m=>`<div class="resource-card"><div class="resource-icon">✧</div><div class="resource-main"><p>${esc(m.content)}</p><small>Saved ${new Date(m.created_at).toLocaleDateString()}</small></div><button class="small-btn danger" data-delete-memory="${m.id}">Remove</button></div>`).join("")||`<p class="muted tiny">No saved memories yet. Add a detail you want to keep.</p>`;
 $$("[data-delete-memory]").forEach(b=>b.onclick=async()=>{await api(`/api/memories/${b.dataset.deleteMemory}`,{method:"DELETE"});loadMemories();});
}
function resizeInput(){const el=$("#messageInput");el.style.height="auto";el.style.height=Math.min(el.scrollHeight,180)+"px";}
async function init(){
 user=await api("/api/me");await loadChats();
 $$(".nav-item").forEach(b=>b.onclick=()=>showView(b.dataset.view));
 $("#newChat").onclick=newChat;$("#refreshChats").onclick=loadChats;
 $("#composer").onsubmit=e=>{e.preventDefault();send($("#messageInput").value)};
 $("#messageInput").addEventListener("input",resizeInput);
 $("#messageInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("#composer").requestSubmit();}});
 $$(".prompt-card").forEach(b=>b.onclick=()=>send(b.dataset.prompt));
 $("#settingsBtn").onclick=$("#topSettings").onclick=()=>showView("settings");
 $("#saveSettings").onclick=saveSettings;
 $("#attachBtn").onclick=$("#chooseFile").onclick=()=>$("#fileInput").click();
 $("#fileInput").onchange=e=>uploadFile(e.target.files[0]);
 $("#uploadZone").ondragover=e=>{e.preventDefault();$("#uploadZone").style.background="#f0efff"};
 $("#uploadZone").ondragleave=()=>$("#uploadZone").style.background="";
 $("#uploadZone").ondrop=e=>{e.preventDefault();$("#uploadZone").style.background="";uploadFile(e.dataTransfer.files[0]);};
 $("#searchBtn").onclick=searchDocuments;$("#searchInput").onkeydown=e=>{if(e.key==="Enter")searchDocuments();};
 $("#saveNote").onclick=async()=>{try{await api("/api/notes",{method:"POST",body:JSON.stringify({id:$("#saveNote").dataset.id||null,title:$("#noteTitle").value||"Untitled",content:$("#noteContent").value})});$("#noteTitle").value="";$("#noteContent").value="";delete $("#saveNote").dataset.id;$("#noteStatus").textContent="Saved just now.";loadNotes();toast("Note saved");}catch(e){toast(e.message)}};
 $("#addMemory").onclick=async()=>{const content=$("#memoryInput").value.trim();if(!content)return;try{await api("/api/memories",{method:"POST",body:JSON.stringify({content})});$("#memoryInput").value="";loadMemories();toast("Memory saved");}catch(e){toast(e.message)}};
 $("#createUser").onclick=async()=>{try{await api("/api/admin/users",{method:"POST",body:JSON.stringify({username:$("#newUsername").value,password:$("#newPassword").value,role:$("#newRole").value})});$("#newUsername").value="";$("#newPassword").value="";$("#adminStatus").textContent="Account created.";loadSettings();}catch(e){$("#adminStatus").textContent=e.message;}};
 $("#menuBtn").onclick=()=>{$("#sidebar").classList.add("open");$("#scrim").classList.add("show")};
 $("#closeSidebar").onclick=()=>{$("#sidebar").classList.remove("open");$("#scrim").classList.remove("show")};
 $("#scrim").onclick=()=>{$("#sidebar").classList.remove("open");$("#scrim").classList.remove("show")};
 document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){e.preventDefault();newChat();}});
}
init().catch(e=>{console.error(e);toast(e.message||"Could not start app");});
