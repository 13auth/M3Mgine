#!/usr/bin/env python3
"""dashboard.py — tek-dosya HTML dashboard (GET /dashboard ile sunulur).

Shell statiktir (secret yok); veri tarayıcıdan authed XHR ile /v1/compliance ve
/v1/rules'tan çekilir (kullanıcı API key'i alana yapıştırır, sessionStorage'da tutulur).
Alıcıya 'göster' demosu: per-rule uyum çubukları + kural listesi + ihlal sayıları.
"""

HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCE — Compliance Dashboard</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;--mut:#8b93a5;
        --ok:#2ecc71;--bad:#e74c3c;--warn:#f1c40f;--accent:#5b8cff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
    align-items:center;gap:14px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
  .badge{font-size:11px;color:var(--mut);border:1px solid var(--line);
    padding:2px 8px;border-radius:999px}
  input{background:var(--card);border:1px solid var(--line);color:var(--fg);
    padding:7px 10px;border-radius:8px;font-size:13px}
  button{background:var(--accent);color:#0a0d14;border:0;padding:8px 14px;
    border-radius:8px;font-weight:650;cursor:pointer}
  main{padding:24px;max-width:1000px;margin:0 auto;display:grid;gap:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
  .card h2{font-size:13px;margin:0 0 14px;color:var(--mut);font-weight:600;
    text-transform:uppercase;letter-spacing:.6px}
  .row{display:flex;align-items:center;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
  .row:first-of-type{border-top:0}
  .rid{flex:0 0 320px;font-family:ui-monospace,Menlo,monospace;font-size:12px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar{flex:1;height:8px;background:#0c0e13;border-radius:6px;overflow:hidden}
  .bar>i{display:block;height:100%}
  .pct{flex:0 0 110px;text-align:right;font-variant-numeric:tabular-nums;color:var(--mut)}
  .tag{font-size:10px;padding:1px 7px;border-radius:999px;border:1px solid var(--line)}
  .hard{color:var(--accent)} .soft{color:var(--warn)}
  .sev-critical,.sev-high{color:var(--bad)} .sev-medium{color:var(--warn)} .sev-low{color:var(--mut)}
  .empty{color:var(--mut);padding:8px 0}
  .err{color:var(--bad)}
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .kpi{background:#12151c;border:1px solid var(--line);border-radius:12px;padding:14px}
  .kpi b{display:block;font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
  .kpi span{color:var(--mut);font-size:12px}
</style></head><body>
<header>
  <h1>CCE · Compliance</h1><span class="badge">correction → enforce → measure</span>
  <span id="usage" class="badge">plan: –</span>
  <span style="flex:1"></span>
  <input id="base" placeholder="API base" style="width:200px">
  <input id="key" type="password" placeholder="Bearer API key" style="width:200px">
  <input id="proj" placeholder="project (ops.)" style="width:120px">
  <button onclick="load()">Yükle</button>
</header>
<main>
  <div class="card"><div class="kpis">
    <div class="kpi"><b id="k-rules">–</b><span>aktif kural</span></div>
    <div class="kpi"><b id="k-comp">–</b><span>ortalama uyum</span></div>
    <div class="kpi"><b id="k-viol">–</b><span>toplam ihlal</span></div>
  </div></div>
  <div class="card"><h2>Kural bazında uyum (canlı)</h2><div id="comp"><div class="empty">Yükle'ye bas.</div></div></div>
  <div class="card"><h2>Uyum trendi (günlük, son 14g)</h2><div id="trend"><div class="empty">—</div></div></div>
  <div class="card"><h2>Kurallar</h2><div id="rules"><div class="empty">—</div></div></div>
</main>
<script>
const $=id=>document.getElementById(id);
$("base").value=localStorage.cce_base||location.origin;
$("key").value=sessionStorage.cce_key||"";
$("proj").value=localStorage.cce_proj||"";
async function api(p){
  const r=await fetch($("base").value.replace(/\\/$/,"")+p,{headers:{Authorization:"Bearer "+$("key").value}});
  if(!r.ok) throw new Error(p+" → "+r.status);
  return r.json();
}
function bar(v){const p=Math.round((v==null?0:v)*100);
  const c=v==null?"var(--mut)":p>=80?"var(--ok)":p>=50?"var(--warn)":"var(--bad)";
  return `<div class="bar"><i style="width:${p}%;background:${c}"></i></div>`;}
async function load(){
  localStorage.cce_base=$("base").value;sessionStorage.cce_key=$("key").value;localStorage.cce_proj=$("proj").value;
  const proj=$("proj").value?("?project="+encodeURIComponent($("proj").value)):"";
  try{
    try{const ug=await api("/v1/usage");
      $("usage").textContent="plan: "+ug.plan+" · "+ug.used+"/"+(ug.limit==null?"∞":ug.limit)+" op";
    }catch(e){}
    const cr=await api("/v1/compliance"), rr=await api("/v1/rules"+proj);
    const comp=cr.compliance||[], rules=rr.rules||[];
    $("k-rules").textContent=rules.length;
    const withc=comp.filter(c=>c.compliance!=null);
    $("k-comp").textContent=withc.length?Math.round(withc.reduce((a,c)=>a+c.compliance,0)/withc.length*100)+"%":"–";
    $("k-viol").textContent=comp.reduce((a,c)=>a+(c.violations||0),0);
    $("comp").innerHTML=comp.length?comp.sort((a,b)=>(a.compliance??1)-(b.compliance??1)).map(c=>
      `<div class="row"><div class="rid" title="${c.rule_id}">${c.rule_id}</div>${bar(c.compliance)}
       <div class="pct">${c.compliance==null?"–":Math.round(c.compliance*100)+"%"} · ${c.passed||0}/${c.checks||0}</div></div>`
    ).join(""):'<div class="empty">Henüz ölçüm yok — check/eval çalıştır.</div>';
    const tr=await api("/v1/compliance/trend?days=14"), trend=tr.trend||[];
    $("trend").innerHTML=trend.length?trend.map(d=>
      `<div class="row"><div class="rid">${d.date}</div>${bar(d.compliance)}
       <div class="pct">${d.compliance==null?"–":Math.round(d.compliance*100)+"%"} · ${d.passed||0}/${d.checks||0}</div></div>`
    ).join(""):'<div class="empty">Henüz veri yok — check/eval çalıştır.</div>';
    $("rules").innerHTML=rules.length?rules.map(r=>
      `<div class="row"><div class="rid" title="${r.id}">${r.id}</div>
       <span class="tag ${r.type}">${r.type}</span>
       <span class="tag sev-${r.severity}">${r.severity}</span>
       <span style="flex:1;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(r.message||"").slice(0,80)}</span></div>`
    ).join(""):'<div class="empty">Kural yok.</div>';
  }catch(e){$("comp").innerHTML='<div class="err">Hata: '+e.message+' (base/key/project doğru mu?)</div>';}
}
if($("key").value) load();
</script>
</body></html>"""
