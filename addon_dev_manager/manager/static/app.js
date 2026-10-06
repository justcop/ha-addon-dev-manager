"use strict";
const base = document.querySelector('meta[name="app-base"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = id => document.getElementById(id);
let snapshot, cardSignature = "", historySignature = "", runtime = {}, refreshing = false;
const versionsCache = new Map();
const discoveryCache = new Map();
let discoveredAddons = [];
try {
  const seed=document.getElementById("default-discovery");
  if(seed) discoveredAddons=JSON.parse(seed.textContent).addons||[];
} catch (_) { discoveredAddons=[]; }
function element(tag, text, className) {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
async function api(path, body) {
  const r=await fetch(base+"/api/"+path,{method:body===undefined?"GET":"POST",headers:body===undefined?{}:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:body===undefined?undefined:JSON.stringify(body)});
  let data;
  try { data=await r.json(); } catch (_) { throw new Error("The manager returned an unreadable response. Restart the add-on and try again."); }
  if(!r.ok)throw new Error(data.error||"Request failed");return data;
}
function toast(message) {$("toast").textContent=message;$("toast").hidden=false;setTimeout(()=>{$("toast").hidden=true;},5000);}
function button(text, action, className="secondary") {const b=element("button",text,className);b.dataset.operation="true";b.addEventListener("click",action);return b;}
async function perform(action, id, sha) {
  const ack=["rollback","recover"].includes(action)||Boolean(sha);
  if(ack&&!confirm("This changes application code only. It does not restore databases, recordings or settings. If a newer version changed its data format, restore a compatible Home Assistant backup first. Continue?"))return;
  try {await api("actions",{action,id,sha,acknowledge_data:ack});$("progress").open=true;await refresh();}catch(e){toast(e.message);}
}
function renderCards() {
  const signature=JSON.stringify([snapshot.targets,snapshot.current,snapshot.transactions,runtime]);
  if(signature===cardSignature)return;cardSignature=signature;
  $("cards").replaceChildren();$("count").textContent=snapshot.targets.length+" configured";
  if(!snapshot.targets.length){const empty=element("div",undefined,"empty");empty.append(element("h3","Add your first repository"),element("p","Choose a GitHub repository, branch and add-on subdirectory in Repositories & settings."));$("cards").append(empty);return;}
  for(const t of snapshot.targets){
    const current=snapshot.current[t.id], tx=snapshot.transactions[t.id], live=current?runtime[current.slug]:undefined;
    const card=element("article",undefined,"card"),head=element("div",undefined,"card-head");
    head.append(element("h3",current?.name||t.id),element("span",tx?"Needs recovery":live?.state||(!t.enabled?"Disabled":current?"Deployed":"Not installed"),"badge "+(tx?"needs_recovery":"")));
    const repo=t.repository.replace("https://github.com/","").replace(/\.git$/,"");
    card.append(head,element("p",repo,"repo"));
    const metadata=element("div",undefined,"metadata");
    metadata.append(element("span","Branch: "+t.branch),element("span","Directory: "+t.path),element("span","Startup: "+(t.enabled&&t.update_on_start?"enabled":"off")));
    card.append(metadata,element("p",current?"Source "+current.source_version+" · "+current.sha.slice(0,12):"No commit deployed yet","code"));
    if(current)card.append(element("p","Container definition: "+current.version,"hint"));
    if(live&&live.version!==current.version)card.append(element("p","Supervisor version differs: "+live.version,"recovery-help"));
    if(tx)card.append(element("p","Interrupted at: "+tx.phase+". Recover before starting another deployment.","recovery-help"));
    const actions=element("div",undefined,"card-actions");
    if(tx){actions.append(button("Recover",()=>perform("recover",t.id),"primary"));}
    else{
      actions.append(button(current?"Update to latest":"Install latest",()=>perform("deploy",t.id),"primary"));
      actions.append(button("Check GitHub",()=>perform("check",t.id)));
      if(current)for(const action of ["start","stop","restart","rollback"])actions.append(button(action[0].toUpperCase()+action.slice(1),()=>perform(action,t.id)));
      actions.append(button("Versions",async()=>{try{versionsCache.set(t.id,await api("targets/"+encodeURIComponent(t.id)+"/versions"));showVersions(card,t);}catch(e){toast(e.message);}}));
    }
    card.append(actions);if(versionsCache.has(t.id))showVersions(card,t);$("cards").append(card);
  }
}
function showVersions(card,t){
  card.querySelector(".versions")?.remove();const rows=versionsCache.get(t.id)||[];
  const box=element("div",undefined,"versions");
  if(!rows.length){box.append(element("p","Use Check GitHub first to load available commits.","hint"));card.append(box);return;}
  const select=element("select");select.setAttribute("aria-label","Commit version for "+t.id);
  for(const row of rows){const option=element("option",row.sha.slice(0,8)+" · "+row.subject);option.value=row.sha;select.append(option);}
  box.append(select,button("Deploy version",()=>perform("deploy",t.id,select.value)));card.append(box);
}
function render(){
  renderCards();const j=snapshot.job;$("job-phase").textContent=j.phase;$("job-status").textContent=j.status;$("job-status").className="badge "+j.status;$("job-dot").className="dot "+j.status;
  $("events").replaceChildren(...j.events.map(e=>{const li=element("li");li.append(element("time",new Date(e.time).toLocaleTimeString()),document.createTextNode(e.message));return li;}));
  const pending=Object.keys(snapshot.transactions).length;$("notice").hidden=!pending;$("notice").textContent=pending+" deployment(s) need recovery. Automatic updates are paused until they are recovered.";
  for(const b of document.querySelectorAll('[data-operation="true"],#update-all,#configure'))b.disabled=snapshot.busy;
  if(pending)$("update-all").disabled=true;
  const hist=JSON.stringify(snapshot.history);if(hist!==historySignature){historySignature=hist;$("history").replaceChildren();for(const h of snapshot.history){const tr=element("tr");const version=element("td",h.source_version);version.title="Container definition: "+h.version;version.append(element("span",h.sha.slice(0,12),"code"));const status=element("td");status.append(element("span",h.status.replaceAll("_"," "),"badge "+h.status));if(h.message)status.title=h.message;tr.append(element("td",h.number),element("td",h.id),version,element("td",new Date(h.time).toLocaleString()),status);$("history").append(tr);}if(!snapshot.history.length){const td=element("td","Your first deployment will appear here.","muted");td.colSpan=5;const tr=element("tr");tr.append(td);$("history").append(tr);}}
}
async function refresh(){if(refreshing)return;refreshing=true;try{snapshot=await api("status");render();}catch(e){$("job-phase").textContent=e.message;$("job-dot").className="dot failed";}finally{refreshing=false;}}
function addRow(t={id:"",repository:"",branch:"main",path:".",enabled:true,update_on_start:true,health_port:0,health_path:"/"}, compact=false){
  const row=element("div",undefined,"repo-row");
  const title=element("div",undefined,"repo-row-title");
  const labelText=t.name||t.path?.split("/").filter(Boolean).pop()||t.id||"Manual entry";
  const titleText=element("div");
  titleText.append(element("strong",labelText),element("span",t.repository?(" · "+String(t.repository).replace("https://github.com/","").replace(/\.git$/,"")):"","muted"));
  title.append(titleText);

  const health=element("label","Health check port","field health-field");
  const healthInput=element("input");
  healthInput.name="health_port";healthInput.type="number";healthInput.min=0;healthInput.max=65535;healthInput.value=t.health_port??0;healthInput.required=true;
  health.append(healthInput);
  const healthHint=element("span","0 disables the HTTP health check.","hint-inline");
  const top=element("div",undefined,"compact-row");
  top.append(title,health,healthHint);
  row.append(top);

  const details=element("details",undefined,"advanced");
  details.open=!compact;
  const summary=element("summary","Advanced settings");
  const fields=element("div",undefined,"fields");
  for(const [key,label,placeholder] of [["id","Unique ID","vinyl"],["repository","GitHub repository","justcop/home-assistant-addons"],["branch","Branch","main"],["path","Add-on subdirectory","vinyl_guardian"],["health_path","HTTP health path","/"]]){
    const l=element("label",label,"field"),input=element("input");input.name=key;input.value=t[key]??"";input.placeholder=placeholder;input.required=true;
    if(key==="id")input.pattern="[a-z][a-z0-9_]{0,39}";
    l.append(input);fields.append(l);
  }
  const footer=element("div",undefined,"row-footer");
  for(const [key,label]of [["enabled","Enabled"],["update_on_start","Update on manager startup"]]){
    const l=element("label",undefined,"check"),input=element("input");input.type="checkbox";input.name=key;input.checked=t[key]!==false;l.append(input,document.createTextNode(label));footer.append(l);
  }
  const remove=element("button","Remove entry","secondary");remove.type="button";remove.addEventListener("click",()=>row.remove());footer.append(remove);
  details.append(summary,fields,footer);row.append(details);$("repo-rows").append(row);return row;
}

function configuredIds(){
  return new Set([...document.querySelectorAll('.repo-row input[name="id"]')].map(i=>i.value).filter(Boolean));
}
function discoveryKey(){return $("discover-repository").value.trim()+"@"+$("discover-branch").value.trim();}
function renderDiscovery(result){
  discoveredAddons=result.addons||[];
  $("discover-repository").value=result.repository||$("discover-repository").value;
  $("discover-branch").value=result.branch||$("discover-branch").value;
  const select=$("discovered-addon");select.replaceChildren();
  if(!discoveredAddons.length){
    select.append(element("option","No add-ons found"));select.disabled=true;$("add-discovered").disabled=true;
    $("discovery-summary").textContent="No folders containing both an add-on config and Dockerfile were found.";$("discovery-summary").hidden=false;return;
  }
  discoveredAddons.forEach(addon=>{const option=element("option",addon.name+" · "+addon.path);option.value=addon.path;option.dataset.healthPort=String(addon.health_port??0);select.append(option);});
  select.disabled=false;$("add-discovered").disabled=false;
  $("discovery-summary").textContent="Found "+discoveredAddons.length+" add-on"+(discoveredAddons.length===1?"":"s")+" in "+result.repository+".";$("discovery-summary").hidden=false;
}
async function discoverRepository(silent=false){
  const repository=$("discover-repository").value.trim(),branch=$("discover-branch").value.trim();
  const summary=$("discovery-summary");
  if(!repository){summary.textContent="Enter a GitHub repository first.";summary.className="discovery-summary error";summary.hidden=false;return;}
  const key=repository+"@"+branch;
  try{
    $("discover-addons").disabled=true;$("discover-addons").textContent="Scanning…";
    summary.textContent="Scanning "+repository+" for Home Assistant add-ons…";summary.className="discovery-summary scanning";summary.hidden=false;
    // Discovery is deliberately GET/read-only. Cache only successful results.
    const query=new URLSearchParams({repository,branch});
    let result=discoveryCache.get(key);
    if(!result){result=await api("discover?"+query.toString());discoveryCache.set(key,result);}
    summary.className="discovery-summary success";
    renderDiscovery(result);
  }catch(e){
    discoveredAddons=[];$("discovered-addon").replaceChildren(element("option","Unable to scan repository"));$("discovered-addon").disabled=true;$("add-discovered").disabled=true;
    summary.textContent="Scan failed: "+e.message;summary.className="discovery-summary error";summary.hidden=false;if(!silent)toast("Repository scan failed: "+e.message);
  }finally{$("discover-addons").disabled=false;$("discover-addons").textContent="Scan repository";}
}
$("configure").addEventListener("click",async()=>{if(!snapshot)return;$("add-result").hidden=true;$("repo-rows").replaceChildren();for(const t of snapshot.targets)addRow(t,true);$("startup-toggle").checked=snapshot.settings.update_on_start;$("automatic-toggle").checked=snapshot.settings.automatic_updates;$("check-interval").value=snapshot.settings.check_interval;$("settings-error").hidden=true;$("settings-dialog").showModal();discoveryCache.clear();await discoverRepository(true);});
$("close-settings").addEventListener("click",()=>$("settings-dialog").close());$("cancel-settings").addEventListener("click",()=>$("settings-dialog").close());$("add-repo").addEventListener("click",()=>addRow(undefined,false));$("discover-addons").addEventListener("click",()=>discoverRepository(false));
$("discovered-addon").addEventListener("change",()=>{const option=$("discovered-addon").selectedOptions[0];if(option)$("discovered-health-port").value=option.dataset.healthPort??0;});
$("settings-form").addEventListener("submit",async e=>{e.preventDefault();const rows=[...document.querySelectorAll(".repo-row")].map(row=>Object.fromEntries([...row.querySelectorAll("input")].map(i=>[i.name,i.type==="checkbox"?i.checked:i.type==="number"?Number(i.value):i.value])));try{await api("settings",{repositories:rows,update_on_start:$("startup-toggle").checked,automatic_updates:$("automatic-toggle").checked,check_interval:Number($("check-interval").value)});$("settings-dialog").close();await refresh();toast("Settings saved to Home Assistant.");}catch(err){$("settings-error").textContent=err.message;$("settings-error").hidden=false;}});
$("update-all").addEventListener("click",()=>perform("deploy_all"));
refresh();setInterval(refresh,1500);async function updateRuntime(){try{runtime=await api("runtime");if(snapshot)render();}catch(e){/* The operation view reports connectivity separately. */}}updateRuntime();setInterval(updateRuntime,10000);
