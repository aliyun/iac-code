"""One-table repository reports, derived from immutable baseline + exact accepted receipts."""

from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .files import WorkflowError, atomic_write, contained, digest, json_write, read_json
from .plans import validate_plan
from .store import Store

LOADED_SHA = digest(Path(__file__))
DATA_DIRECTORY = Path(__file__).resolve().parent

LABELS = {"ROS": "ROS", "TF": "ROS 托管 Terraform", "CREATE": "创建 API", "QUERY": "查询 API"}
STATUS = {
    "pass": "✓ 通过",
    "fail": "× 未通过",
    "unsupported": "不支持 iframe",
    "review": "? 待核",
    "untested": "— 未测",
    "no_sample": "覆盖范围无样本",
    "execution_gap": "执行遗漏",
    "product_gap": "产品缺陷",
}
CSS = """
:root{color-scheme:light;--ink:#243247;--muted:#64748b;--line:#dce4ed;--bg:#fff}
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:var(--ink);font:14px/1.6 system-ui,sans-serif}
main{max-width:1560px;margin:28px auto;padding:0 24px}h1{font-size:24px;margin:0 0 6px}
p{color:var(--muted);margin:0 0 18px}input{width:360px;max-width:100%;padding:10px 14px;margin:0 0 16px;
border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:var(--ink)}
.table-wrap{overflow:auto;max-height:80vh;border:1px solid var(--line);border-radius:10px;background:#fff}
table{border-collapse:separate;border-spacing:0;width:100%;min-width:1000px;table-layout:fixed}
th,td{padding:12px 16px;text-align:left;border-bottom:1px solid var(--line);background:#fff;color:var(--ink);
overflow-wrap:anywhere;vertical-align:middle}thead th{position:sticky;top:0;z-index:2;
background:#eaf0f7;font-weight:650}
th.service{background:#f0f4f9;border-right:1px solid var(--line);vertical-align:top;padding-top:16px}
th.resource{border-right:1px solid var(--line)}.new-resource>*{border-top:1px solid #cbd5e1}
.sub{display:block;font-size:12px;color:var(--muted);font-weight:400;margin-top:3px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;white-space:nowrap;font-weight:600;font-size:13px}
.pass{background:#e9f6ee;color:#166534}.fail,.product_gap{background:#fff0ef;color:#ad2920}
.untested,.no_sample{background:#f1f4f8;color:#667085}.unsupported{background:#eef1fb;color:#49558f}
.review,.execution_gap{background:#fff6df;color:#8b6100}.reason{font-size:13px;color:#526176}
.lifecycle{font-size:12px}.lifecycle .badge{white-space:normal}.reason details{margin-top:5px}
.reason summary{cursor:pointer;color:#476d9b}.reason details p{margin:8px 0;font-size:12px}
a{color:#3268a8;text-underline-offset:3px}.evidence-links{display:flex;flex-wrap:wrap;gap:4px 12px}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--ink:#e1e8f2;--muted:#a6b3c7;--line:#334155}
body{background:#111827}th,td,input,.table-wrap{background:#1b2535;color:var(--ink)}
thead th,th.service{background:#263448}.sub,p,.reason{color:#b0bed1}.new-resource>*{border-top-color:#526078}
.reason summary,a{color:#a7c9fa}
.pass{background:#163c2c;color:#8de0b1}.fail,.product_gap{background:#482725;color:#ffb1aa}
.untested,.no_sample{background:#2b3648;color:#bbc6d6}.unsupported{background:#303451;color:#c1c7f4}
.review,.execution_gap{background:#473c24;color:#f4d48c}}
[hidden]{display:none!important}
.product-tabs{display:flex;flex-wrap:wrap;gap:7px;padding:16px;margin:20px 0;border:1px solid var(--line);
border-radius:12px;background:var(--bg)}
.product-tab{appearance:none;font:inherit;cursor:pointer;border:1px solid transparent;border-radius:7px;
padding:7px 12px;color:var(--muted);background:transparent;display:flex;align-items:center;gap:8px;white-space:nowrap}
.product-tab:hover{background:#edf2f8;color:var(--ink)}
.product-tab[aria-selected=true]{background:#2563eb;color:#fff;font-weight:650}
.product-tab:focus-visible,input:focus-visible{outline:3px solid #60a5fa;outline-offset:2px}
.tab-count{font-size:11px;opacity:.55;padding:1px 6px;border:1px solid transparent;border-radius:6px;
font-variant-numeric:tabular-nums}
.tab-count[data-nonzero=true]{font-size:13px;font-weight:800;opacity:1;color:#1d4ed8;
background:#dbeafe;border-color:#bfdbfe}
.product-tab[aria-selected=true] .tab-count[data-nonzero=true]{color:#fff;
background:#ffffff26;border-color:#ffffff55}
.product-heading{display:flex;align-items:baseline;gap:14px;margin:24px 0 12px}
.product-heading h2{font-size:23px;margin:0}.product-heading span{color:var(--muted);font-size:13px}
.metrics{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin:0 0 10px}
.metric{padding:12px 16px;border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.metric span{display:block;font-size:12px;color:var(--muted)}.metric strong{font-size:25px;line-height:1.5}
.metric .creation-metric{margin-top:4px;padding-top:5px;border-top:1px solid var(--line)}
.metric .creation-metric b{font-size:14px;color:var(--ink);margin-left:6px}
.metric[data-status=pass] strong{color:#15803d}.metric[data-status=fail] strong{color:#b91c1c}
.metric[data-status=unsupported] strong{color:#5366a4}.metric[data-status=execution_gap] strong{color:#a16207}
.count-note{font-size:12px;margin-bottom:18px}.product-toolbar{display:flex;align-items:baseline;gap:16px}
.product-toolbar .filter-note{color:var(--muted);font-size:12px}
.empty-message{padding:24px;text-align:center;color:var(--muted)}
.report-filters{border:1px solid var(--line);border-radius:12px;background:var(--bg);padding:10px 16px;margin:18px 0}
.filter-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:7px 0}
.filter-row+.filter-row{border-top:1px solid var(--line)}
.filter-label{min-width:76px;font-size:12px;color:var(--muted);font-weight:600}
.filter-button{appearance:none;font:inherit;cursor:pointer;padding:5px 12px;border:1px solid transparent;
border-radius:6px;background:transparent;color:var(--muted)}
.filter-button:hover{background:#edf2f8;color:var(--ink)}
.filter-button[aria-pressed=true]{background:#2563eb;color:white;font-weight:600}
.filter-button:focus-visible{outline:3px solid #60a5fa;outline-offset:2px}
.filter-summary{font-size:12px;margin:8px 0 0;color:var(--muted)}
.filter-totals{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px;padding-top:12px;
border-top:1px solid var(--line)}
.totals-label{font-size:12px;color:var(--muted);margin-right:4px}
.total-item{display:inline-flex;align-items:baseline;gap:6px;padding:3px 10px;border:1px solid var(--line);
border-radius:7px;font-size:12px;color:var(--muted)}
.total-item strong{font-size:19px;line-height:1.5;font-weight:800;color:#1d4ed8;font-variant-numeric:tabular-nums}
.total-item[data-nonzero=false] strong{color:var(--muted);font-weight:500}
td.filtered-out .badge{opacity:.4}
td.filtered-out::after{content:'不计入筛选';display:block;font-size:10px;color:var(--muted)}
@media(max-width:900px){.metrics{grid-template-columns:repeat(4,minmax(0,1fr))}main{padding:0 14px}
.product-toolbar{display:block}.product-tabs{padding:10px;flex-wrap:nowrap;overflow-x:auto}
.product-tab{padding:6px 9px;flex-shrink:0}}
@media(max-width:520px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.product-heading{display:block}}
@media(prefers-color-scheme:dark){:root{--bg:#1b2535}.product-tab:hover{background:#263448}
.tab-count[data-nonzero=true]{color:#9bd9ff;background:#173d60;border-color:#3b7099}
.total-item[data-nonzero=true] strong{color:#9bd9ff}
.filter-button:hover{background:#263448;color:var(--ink)}
.metric[data-status=pass] strong{color:#8de0b1}.metric[data-status=fail] strong{color:#ffb1aa}
.metric[data-status=unsupported] strong{color:#c1c7f4}.metric[data-status=execution_gap] strong{color:#f4d48c}}
"""

RESULTS_SCRIPT = """
(()=>{
const tabs=[...document.querySelectorAll('[role=tab]')];
const panels=[...document.querySelectorAll('[role=tabpanel]')];
const search=document.querySelector('#search');
const filters={source:'all',mode:'all',status:'all'};
const modes=['iframe','open','creation'];
const statusKeys=['pass','fail','untested','unsupported','execution_gap','review','no_sample'];
const emptyCounts=()=>Object.fromEntries(statusKeys.map(key=>[key,0]));
const normalized=status=>status==='product_gap'?'fail':status;
const records=panels.map(panel=>({panel,groups:[...panel.querySelectorAll('tbody[data-search]')].map(group=>({
  group,header:group.querySelector('.resource'),rows:[...group.querySelectorAll('tr')]
}))}));
function matchingModes(row){
  return modes.filter(mode=>(filters.mode==='all'||filters.mode===mode)&&row.dataset[mode]&&
    (filters.status==='all'||normalized(row.dataset[mode])===filters.status));
}
function filterResources(){
  const q=search.value.trim().toLowerCase();
  const totals={services:0,resources:0,sources:0,clicks:0,creations:0};
  for(const {panel,groups} of records){
    let visible=0,sources=0,clicks=0,creations=0;
    const clickCounts=emptyCounts(),creationCounts=emptyCounts();
    for(const {group,header,rows} of groups){
      const seenCreations=new Set(),shown=[];
      for(const row of rows){
        const matches=matchingModes(row);
        const show=group.dataset.search.includes(q)&&(filters.source==='all'||row.dataset.dimension===filters.source)
          &&matches.length>0;
        row.hidden=!show;row.classList.remove('new-resource');
        for(const cell of row.querySelectorAll('td[data-mode]')){
          const excluded=show&&Boolean(row.dataset[cell.dataset.mode])&&!matches.includes(cell.dataset.mode);
          cell.classList.toggle('filtered-out',excluded);
        }
        if(!show)continue;
        shown.push(row);sources++;
        for(const mode of matches){
          const status=normalized(row.dataset[mode]);
          if(mode==='creation'){
            if(seenCreations.has(row.dataset.dimension))continue;
            seenCreations.add(row.dataset.dimension);creationCounts[status]++;creations++;
          }else{clickCounts[status]++;clicks++;}
        }
      }
      group.hidden=shown.length===0;
      if(shown.length){
        visible++;shown[0].prepend(header);header.rowSpan=shown.length;shown[0].classList.add('new-resource');
      }
    }
    const tab=document.getElementById(panel.getAttribute('aria-labelledby'));
    const badge=tab.querySelector('.tab-count');badge.textContent=visible+' 种';
    badge.dataset.nonzero=String(visible>0);
    tab.title=visible+' 种资源 · '+sources+' 个来源子项 · '+clicks+' 项点击 · '+creations+' 项创建';
    let summary=visible+' 种资源 · '+sources+' 个来源子项';
    const kind=filters.mode==='iframe'?' iframe ':filters.mode==='open'?'直接打开':'点击';
    if(filters.mode!=='creation')summary+=' · '+clicks+' 项'+kind+'验收';
    if(filters.mode==='all'||filters.mode==='creation')summary+=' · '+creations+' 项创建验收';
    panel.querySelector('.product-heading span').textContent=summary;
    for(const card of panel.querySelectorAll('.metric')){
      const status=card.dataset.status;
      card.querySelector('span').textContent=card.dataset.label+(filters.mode==='creation'?' · 创建':' · 点击');
      card.querySelector('strong').textContent=(filters.mode==='creation'?creationCounts:clickCounts)[status];
      const extra=card.querySelector('.creation-metric');extra.hidden=filters.mode!=='all';
      extra.querySelector('b').textContent=creationCounts[status];
      card.title=filters.mode==='all'?
        '点击 '+clickCounts[status]+' 项；创建 '+creationCounts[status]+' 项（分别计数）':'';
    }
    for(const cell of panel.querySelectorAll('[data-mode]')){
      cell.hidden=filters.mode!=='all'&&cell.dataset.mode!==filters.mode;
    }
    panel.querySelector('.empty-message').hidden=visible>0;
    panel.querySelector('.filter-note').textContent='当前筛选：'+visible+' 种资源；所有统计与表格同步。';
    panel.querySelector('.count-note').textContent=filters.mode==='creation'?
      '创建按资源类型 × 来源维度各计 1 项；查询 API 不计创建，清理不计入数量。创建成功不新增点击通过。':
      '点击按每个来源的 iframe / 直接打开分别计数；创建按资源类型 × 来源维度计数，两者分开、不相加。'+
      '未通过包含产品缺陷，清理不计数。';
    totals.services+=Number(visible>0);totals.resources+=visible;totals.sources+=sources;
    totals.clicks+=clicks;totals.creations+=creations;
  }
  for(const item of document.querySelectorAll('.filter-totals [data-total]')){
    const key=item.dataset.total;item.querySelector('strong').textContent=totals[key];
    item.dataset.nonzero=String(totals[key]>0);
    item.hidden=key==='clicks'&&filters.mode==='creation'||key==='creations'&&
      filters.mode!=='all'&&filters.mode!=='creation';
    if(key==='clicks')item.querySelector('span').textContent=filters.mode==='iframe'?'项 iframe 验收':
      filters.mode==='open'?'项直接打开验收':'项点击验收';
  }
  const labels=[...document.querySelectorAll('.filter-button[aria-pressed=true]')].map(button=>button.textContent);
  const summary=document.querySelector('.filter-summary');
  if(summary)summary.textContent='当前范围：'+labels.join(' / ')+(q?' · 搜索：'+search.value.trim():'');
}
function selectProduct(tab,focus=false){
  for(const item of tabs){const selected=item===tab;
    item.setAttribute('aria-selected',String(selected));item.tabIndex=selected?0:-1;
  }
  for(const panel of panels)panel.hidden=panel.id!==tab.getAttribute('aria-controls');
  tab.scrollIntoView({block:'nearest',inline:'nearest'});
  if(focus)tab.focus();
  filterResources();
}
for(const tab of tabs){
  tab.addEventListener('click',()=>selectProduct(tab));
  tab.addEventListener('keydown',event=>{
    let index=tabs.indexOf(tab);
    if(event.key==='ArrowRight')index=(index+1)%tabs.length;
    else if(event.key==='ArrowLeft')index=(index-1+tabs.length)%tabs.length;
    else if(event.key==='Home')index=0;
    else if(event.key==='End')index=tabs.length-1;
    else return;
    event.preventDefault();selectProduct(tabs[index],true);
  });
}
search.addEventListener('input',filterResources);
for(const button of document.querySelectorAll('.filter-button')){
  button.addEventListener('click',()=>{
    const key=button.dataset.filter;filters[key]=button.dataset.value;
    for(const other of document.querySelectorAll('.filter-button[data-filter="'+key+'"]')){
      other.setAttribute('aria-pressed',String(other===button));
    }
    filterResources();
  });
}
filterResources();
})();
"""


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def evidence_link(root: Path, destination: Path, name: str, label: str) -> str:
    """Only link local, contained artifacts; never embed private artifact contents or URLs."""
    try:
        path = contained(root, name)
        if not name or not path.is_file():
            return ""
        href = quote(Path(os.path.relpath(path, destination.parent)).as_posix(), safe="/")
    except (WorkflowError, ValueError):
        return ""
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(label)}</a>'


def cleanup_summary(root: Path, owners: list[Any], destination: Path) -> dict[str, Any]:
    if not owners:
        return {"status": "untested", "label": "无清理验收记录", "links": []}
    links, pending, missing, outstanding = [], False, False, False
    for owner in owners:
        if owner["state"] != "deleted":
            outstanding = True
            continue
        try:
            binding = json.loads(owner["evidence"])
            path = contained(root, binding["path"])
            if digest(path) != binding["sha256"]:
                raise WorkflowError("cleanup proof changed")
            proof = read_json(path)
            if (
                proof.get("ownership_id") != owner["id"]
                or proof.get("resource") != json.loads(owner["resource"])
                or proof.get("failed") is not False
            ):
                raise WorkflowError("cleanup proof does not bind owner")
            source = contained(root, proof["source_evidence"])
            if digest(source) != proof["source_sha256"]:
                raise WorkflowError("cleanup source changed")
            accepted_pending = proof.get("kind") == "ops_user_accepted_pending_delete"
            if accepted_pending:
                from .pending_delete import validate

                validate(root, owner["task_id"], owner["id"], json.loads(owner["resource"]), proof)
                pending = True
            elif proof.get("absent") is not True:
                raise WorkflowError("physical absence not verified")
            links.append(evidence_link(root, destination, binding["path"], "清理依据"))
        except (OSError, ValueError, KeyError, TypeError):
            missing = True
    status, label = (
        ("execution_gap", "仍有资源未清理") if outstanding
        else ("review", "清理证据待核") if missing
        else ("review", "PendingDelete · 按授权收口") if pending
        else ("pass", "✓ 已核验清理")
    )
    return {"status": status, "label": label, "links": links,
            "note": "PendingDelete 按用户授权视为已删除；不代表已物理消失。" if pending else ""}


def creation_status(row: dict[str, Any]) -> str:
    """Query navigation and explicitly inapplicable creation are not untested writes."""
    creation = row.get("creation", {})
    if row["dimension"] == "QUERY" or creation.get("label", "").startswith("不适用"):
        return ""
    return creation.get("status", "untested")


def filter_controls() -> str:
    choices = (
        ("source", "来源", (("all", "全部"), ("ROS", "ROS"), ("TF", "Terraform"),
                            ("CREATE", "创建 API"), ("QUERY", "查询 API"))),
        ("mode", "验收方式", (("all", "全部"), ("iframe", "iframe"), ("open", "直接打开"),
                               ("creation", "创建验收"))),
        ("status", "结果状态", (("all", "全部"), ("pass", "通过"), ("fail", "未通过"),
                                 ("untested", "未测"), ("unsupported", "不支持 iframe"),
                                 ("execution_gap", "执行遗漏"), ("review", "待核"), ("no_sample", "范围无样本"))),
    )
    rows = []
    for key, label, options in choices:
        buttons = "".join(
            f'<button class="filter-button" type="button" data-filter="{key}" data-value="{value}" '
            f'aria-pressed="{str(value == "all").lower()}">{text}</button>'
            for value, text in options
        )
        rows.append(f'<div class="filter-row" role="group" aria-label="按{label}筛选">'
                    f'<span class="filter-label">{label}</span>{buttons}</div>')
    return '<div class="report-filters">' + "".join(rows) + (
        '<p class="filter-summary" aria-live="polite">当前范围：全部 / 全部 / 全部</p>'
        '<div class="filter-totals" role="status" aria-label="当前筛选总计">'
        '<span class="totals-label">当前筛选总计 · 所有服务</span>'
        + "".join(
            f'<span class="total-item" data-total="{key}"><strong>—</strong><span>{label}</span></span>'
            for key, label in (("services", "个服务"), ("resources", "种资源"), ("sources", "个来源子项"),
                               ("clicks", "项点击验收"), ("creations", "项创建验收"))
        ) + '</div></div>'
    )


def document(title: str, groups: list[dict[str, Any]], *, matrix: bool = False) -> str:
    services: dict[str, list[dict[str, Any]]] = {}
    for group in sorted(groups, key=lambda row: (row["service"].casefold(), row["name"].casefold())):
        services.setdefault(group["service"], []).append(group)
    rows = []
    tabs, panels = [], []
    for service_index, (service, resources) in enumerate(services.items()):
        service_count = sum(len(resource["rows"]) for resource in resources)
        first_service = True
        service_rows = []
        for resource in resources:
            resource_rows = []
            for index, row in enumerate(resource["rows"]):
                cells = []
                if first_service and matrix:
                    cells.append(
                        f'<th scope="rowgroup" rowspan="{service_count}" class="service">{escape(service)}</th>'
                    )
                    first_service = False
                if index == 0:
                    ident = f'<span class="sub">{escape(resource.get("id", ""))}</span>' if matrix else ""
                    cells.append(
                        f'<th scope="rowgroup" rowspan="{len(resource["rows"])}" class="resource">'
                        f"{escape(resource['name'])}{ident}</th>"
                    )
                label = LABELS.get(row["dimension"], row["dimension"])
                if row["dimension"] == "TF" and not matrix:
                    managed_modes = set(row.get("ros_managed_modes", []))
                    label = (
                        LABELS["TF"] if managed_modes == {"iframe", "open"}
                        else "ROS 托管 Terraform / 历史入口" if managed_modes
                        else "ROS 托管 Terraform" if row.get("creation", {}).get("managed")
                        else "Terraform（历史入口）"
                    )
                cells.append(f'<td>{escape(label)}<span class="sub">{escape(row["source_type"])}</span></td>')
                for mode in ("iframe", "open"):
                    status = row["status"][mode]
                    attribute = "" if matrix else f' data-mode="{mode}"'
                    cells.append(
                        f'<td{attribute}><span class="badge {escape(status)}">{escape(STATUS[status])}</span></td>'
                    )
                if not matrix:
                    creation = row.get("creation", {})
                    cleanup = row.get("cleanup", {})
                    for value, default, attribute in (
                        (creation, "— 未验收", ' data-mode="creation"'), (cleanup, "无清理验收记录", ""),
                    ):
                        cells.append(
                            f'<td class="lifecycle"{attribute}>'
                            f'<span class="badge {escape(value.get("status", "untested"))}">'
                            f'{escape(value.get("label", default))}</span></td>'
                        )
                    note = cleanup.get("note", "")
                    if row.get("creation", {}).get("managed") and not row.get("ros_managed_modes"):
                        note = "创建：ROS 托管 Terraform；点击：沿用历史入口结果，未复测。" + note
                    links = creation.get("links", []) + cleanup.get("links", [])
                    detail = escape(row["reason"])
                    if creation.get("reason"):
                        detail += "</p><p>创建验收：" + escape(creation["reason"])
                    cells.append(
                        f'<td class="reason">{escape(note)}<details><summary>查看验收依据</summary>'
                        f'<p>{detail}</p><div class="evidence-links">{"".join(links)}</div></details></td>'
                    )
                else:
                    cells.append(f'<td class="reason">{escape(row["reason"])}</td>')
                attributes = "" if matrix else (
                    f' data-dimension="{escape(row["dimension"])}"'
                    f' data-iframe="{escape(row["status"]["iframe"])}" data-open="{escape(row["status"]["open"])}"'
                    f' data-creation="{escape(creation_status(row))}"'
                )
                resource_rows.append(
                    f'<tr class="{"new-resource" if index == 0 else ""}"{attributes}>{"".join(cells)}</tr>'
                )
            rows.extend(resource_rows)
            search_text = f"{service} {resource['name']} {resource.get('key', '')}".casefold()
            service_rows.append(f'<tbody data-search="{escape(search_text)}">{"".join(resource_rows)}</tbody>')
        if not matrix:
            counts = {status: 0 for status in STATUS}
            creation_counts = {status: 0 for status in STATUS}
            for resource in resources:
                seen = set()
                for row in resource["rows"]:
                    for mode in ("iframe", "open"):
                        counts[row["status"][mode]] += 1
                    status = creation_status(row)
                    if status and row["dimension"] not in seen:
                        seen.add(row["dimension"])
                        creation_counts[status] += 1
            creation_total = sum(creation_counts.values())
            creation_counts["fail"] += creation_counts["product_gap"]
            metrics = (
                ("pass", "通过", counts["pass"], ""),
                ("fail", "未通过", counts["fail"] + counts["product_gap"],
                 f"未通过 {counts['fail']} 项；产品缺陷 {counts['product_gap']} 项"),
                ("untested", "未测", counts["untested"], ""),
                ("unsupported", "不支持 iframe", counts["unsupported"], ""),
                ("execution_gap", "执行遗漏", counts["execution_gap"], ""),
                ("review", "待核", counts["review"], ""),
                ("no_sample", "范围无样本", counts["no_sample"], ""),
            )
            cards = "".join(
                f'<div class="metric" data-status="{status}" data-label="{label}" title="{escape(note)}">'
                f'<span>{label} · 点击</span><strong>{count}</strong>'
                f'<span class="creation-metric">创建<b>{creation_counts[status]}</b></span></div>'
                for status, label, count, note in metrics
            )
            selected = service_index == 0
            tabs.append(
                f'<button class="product-tab" type="button" role="tab" id="tab-{service_index}" '
                f'aria-controls="product-{service_index}" aria-selected="{str(selected).lower()}" '
                f'tabindex="{0 if selected else -1}">{escape(service)}'
                f'<span class="tab-count" data-nonzero="{str(bool(resources)).lower()}">'
                f'{len(resources)} 种</span></button>'
            )
            panels.append(
                f'<section role="tabpanel" id="product-{service_index}" aria-labelledby="tab-{service_index}"'
                f'{"" if selected else " hidden"}><div class="product-heading"><h2>{escape(service)}</h2>'
                f'<span>{len(resources)} 种资源 · {service_count} 个来源子项 · {sum(counts.values())} 项点击验收'
                f' · {creation_total} 项创建验收</span>'
                f'</div><div class="metrics">{cards}</div>'
                '<p class="count-note">累计结果：每个来源的 iframe 与直接打开各计 1 项；未通过包含产品缺陷。'
                '创建与清理不计入这些数量。创建验收另列，按资源类型 × 来源维度各计 1 项。</p>'
                '<div class="product-toolbar"><span class="filter-note" aria-live="polite"></span></div>'
                '<div class="table-wrap"><table><colgroup><col style="width:15%"><col style="width:21%">'
                '<col data-mode="iframe" style="width:12%"><col data-mode="open" style="width:12%">'
                '<col data-mode="creation" style="width:11%">'
                '<col style="width:13%"><col style="width:16%"></colgroup><thead><tr><th>资源类型</th>'
                '<th>来源 / 子项</th><th data-mode="iframe">iframe · 普通点击</th>'
                '<th data-mode="open">直接打开 · Shift</th><th data-mode="creation">创建验收</th>'
                f'<th>清理</th><th>说明</th></tr></thead>{"".join(service_rows)}</table>'
                '<div class="empty-message" hidden>该产品没有匹配的资源类型。</div></div></section>'
            )
    subtitle = (
        "固定编号；每种资源四个来源维度。历史链接通过保留，创建链须另外验证。"
        if matrix
        else "创建验收与点击验收分开列示；历史通过保留，不因创建成功新增点击通过。未测不表示没有云资源。"
    )
    columns = (
        '<col style="width:12%"><col style="width:17%"><col style="width:23%">'
        '<col style="width:13%"><col style="width:13%"><col style="width:22%">'
        if matrix else
        '<col style="width:9%"><col style="width:12%"><col style="width:19%">'
        '<col style="width:11%"><col style="width:11%"><col style="width:10%">'
        '<col style="width:12%"><col style="width:16%">'
    )
    lifecycle_headers = "" if matrix else "<th>创建验收</th><th>清理</th>"
    if not matrix:
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head><body><main><h1>{escape(title)}</h1><p>{subtitle}</p>
{filter_controls() if panels else ""}
<nav class="product-tabs" role="tablist" aria-label="按产品查看验收结果">{"".join(tabs)}</nav>
<input id="search" type="search" placeholder="搜索服务 / 资源类型" aria-label="筛选资源类型">
{"".join(panels) if panels else '<p class="empty-message">暂无验收结果。</p>'}</main>
<script>{RESULTS_SCRIPT}</script></body></html>"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head><body><main><h1>{escape(title)}</h1><p>{subtitle}</p>
<input id="search" type="search" placeholder="筛选服务 / 资源类型" aria-label="筛选服务或资源类型">
<div class="table-wrap"><table><colgroup>{columns}</colgroup>
<thead><tr><th>服务</th><th>资源类型</th><th>来源 / 子项</th>
<th>iframe · 普通点击</th><th>直接打开 · Shift</th>{lifecycle_headers}<th>说明</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div></main>
<script>const rows=[...document.querySelectorAll('tbody tr')];let service='',resource='';
for(const row of rows){{if(row.querySelector('.service'))service=row.querySelector('.service').textContent;
if(row.querySelector('.resource'))resource=row.querySelector('.resource').textContent;
row.dataset.search=(service+' '+resource).toLowerCase();}}
// Hide whole service groups: retained rowspans never become detached/misaligned.
document.querySelector('#search').addEventListener('input',event=>{{const q=event.target.value.trim().toLowerCase();
let group=[];const flush=()=>{{const show=group.some(row=>row.dataset.search.includes(q));
for(const row of group)row.hidden=!show;}};
for(const row of rows){{if(row.querySelector('.service')){{flush();group=[];}}group.push(row);}}flush();}});
</script></body></html>"""


def make_plan(
    catalog: Path, plan_id: str, ids: list[str], destination: Path | None, store: Store | None = None,
    initial_wave: int = 1,
    retest_scenes: list[str] | None = None,
) -> dict[str, Any]:
    registry = read_json(catalog)
    by_id = {item["id"]: item for item in registry["resources"]}
    if len(ids) != len(set(ids)) or set(ids) - set(by_id):
        raise WorkflowError("resource IDs must exist and be unique; add new IDs to catalog first")
    plan = {"schema_version": 1, "plan_id": plan_id, "resources": [copy.deepcopy(by_id[key]) for key in ids]}
    plan["initial_wave"] = initial_wave
    for resource in plan["resources"]:
        resource["execution_policy_version"] = 2
    if store:
        with store.transaction() as connection:
            previous = list(connection.execute("SELECT resource_id,spec FROM tasks ORDER BY rowid"))
        for resource in plan["resources"]:
            for row in previous:
                if row["resource_id"] != resource["id"]:
                    continue
                prior = json.loads(row["spec"])
                for dimension, source in prior["dimensions"].items():
                    current = resource["dimensions"][dimension]
                    if source.get("creation_verified"):
                        current["creation_verified"] = True
                    for old in source["contracts"]:
                        match = next(
                            (
                                new
                                for new in current["contracts"]
                                if new["id"] == old["id"] and new["source_type"] == old["source_type"]
                            ),
                            None,
                        )
                        if match:
                            for mode, status in old["historical"].items():
                                if status in {"pass", "unsupported"}:
                                    match["historical"][mode] = status
    if retest_scenes is not None:
        if (not retest_scenes or any(not isinstance(scene, str) for scene in retest_scenes)
                or len(set(retest_scenes)) != len(retest_scenes)):
            raise WorkflowError("targeted plan requires unique nonempty scene IDs")
        unmatched = set(retest_scenes)
        for resource in plan["resources"]:
            targets = [scene for scene in retest_scenes if scene.startswith(resource["id"] + "-")]
            if not targets:
                raise WorkflowError("every targeted resource needs its own exact failed scene")
            unmatched.difference_update(targets)
            resource["retest_scope"] = {
                "kind": "failed_query_navigation" if len(ids) == 1 and all(
                    scene.startswith(resource["id"] + "-QUERY-") for scene in targets
                ) else "failed_navigation_existing",
                "scene_ids": targets,
            }
        if unmatched:
            raise WorkflowError("targeted scene belongs to an unselected resource")
    validate_plan(plan)
    if destination is not None:
        if destination.exists():
            raise WorkflowError("plans are immutable; choose a new output path")
        json_write(destination, plan)
    return plan


def make_review_plan(catalog: Path, review_path: Path, plan_id: str, destination: Path, store: Store) -> None:
    """Freeze the review's exact failures against fresh accepted positive checkpoints."""
    review = read_json(review_path)
    from .remaining_review import REVIEW_KIND, make_remaining_plan

    if review.get("kind") == REVIEW_KIND:
        make_remaining_plan(catalog, review_path, plan_id, destination, store)
        return
    if (review.get("kind") != "resource_link_targeted_retest_review"
            or review.get("dispatch_allowed") is not False or not review.get("targets")):
        raise WorkflowError("expected an immutable targeted retest review, not an executable plan")
    ids = [target["resource_id"] for target in review["targets"]]
    baseline = make_plan(catalog, plan_id, ids, None, store)
    by_id = {resource["id"]: resource for resource in baseline["resources"]}
    selected, excluded, retained = [], [], []
    for target in review["targets"]:
        resource = by_id[target["resource_id"]]
        if target["key"] != resource["key"] or target["dimension"] not in {"QUERY", "ROS"}:
            raise WorkflowError("review must retain canonical identity and supported source dimension")
        source = resource["dimensions"][target["dimension"]]
        contract = next((c for c in source["contracts"] if c["id"] == target["contract_id"]
                         and c["source_type"] == target["source_type"]), None)
        if contract is None:
            raise WorkflowError("review contract/source differs from permanent identity")
        own = []
        for scene in target["target_scene_ids"]:
            mode = scene.removeprefix(contract["id"] + "-")
            if mode not in {"iframe", "open"}:
                raise WorkflowError("review contains a cross-contract or non-navigation scene")
            status = contract["historical"][mode]
            if status in {"pass", "unsupported"}:
                excluded.append({"scene_id": scene, "status": status, "reason": "retained positive checkpoint"})
            elif status == "fail":
                own.append(scene)
            else:
                raise WorkflowError("review cannot expand into an untested or unverified scene")
        if own:
            retained.append(resource["id"])
            selected.extend(own)
    if not selected:
        raise WorkflowError("all reviewed targets already positive; no execution plan to create")
    plan = make_plan(catalog, plan_id, retained, None, store, initial_wave=4, retest_scenes=selected)
    plan["review_lineage"] = {
        "plan_id": review["plan_id"], "path": str(review_path.resolve()), "sha256": digest(review_path),
        "excluded_scenes": excluded,
        "baseline": {path.name: digest(path) for path in (
            catalog, catalog.with_name("historical-results.json"), catalog.with_name("resource-link-results.html"),
        )},
    }
    if destination.exists():
        raise WorkflowError("plans are immutable; choose a new output path")
    json_write(destination, plan)


def add_resource(catalog: Path, resource: dict[str, Any]) -> str:
    from .runtime import exclusive

    with exclusive(catalog.with_suffix(".lock")):
        registry = read_json(catalog)
        item = copy.deepcopy(resource)
        next_id = max(int(value["id"][1:]) for value in registry["resources"]) + 1
        item["id"] = f"R{next_id:04d}"
        for dimension, source in item["dimensions"].items():
            if source.get("creation_verified"):
                raise WorkflowError("a newly registered resource cannot claim a verified creation chain")
            for index, contract in enumerate(source.get("contracts", []), 1):
                contract["id"] = f"{item['id']}-{dimension}-{index}"
                if any(status != "untested" for status in contract["historical"].values()):
                    raise WorkflowError("new resource cannot manufacture historical results")
                baseline = catalog.parent / "historical-results.json"
                if baseline.exists():
                    matches = [
                        row
                        for group in read_json(baseline)["groups"]
                        if group["key"] == item["key"]
                        for row in group["rows"]
                        if row["dimension"] == dimension and row["source_type"] == contract["source_type"]
                    ]
                    if len(matches) == 1:
                        contract["historical"] = copy.deepcopy(matches[0]["status"])
        registry["resources"].append(item)
        validate_plan(registry)
        json_write(catalog, registry)
    return item["id"]


def render_matrix(catalog: Path, destination: Path) -> None:
    groups = []
    for spec in read_json(catalog)["resources"]:
        rows = []
        for dimension, source in spec["dimensions"].items():
            for contract in source["contracts"]:
                rows.append(
                    {
                        "dimension": dimension,
                        "source_type": contract["source_type"],
                        "status": contract["historical"],
                        "reason": "适用性待官方证据确认；创建后需清理。"
                        if dimension != "QUERY"
                        else "只读查询；复用有效原始来源。",
                    }
                )
        groups.append({**spec, "rows": rows})
    atomic_write(destination, document("资源链接测试矩阵", groups, matrix=True))


def render_results(store: Store | None, destination: Path) -> None:
    directory = DATA_DIRECTORY
    baseline = read_json(directory / "historical-results.json")
    groups = baseline["groups"]
    contracts = {row["contract_id"]: row for group in groups for row in group["rows"]}
    registry = read_json(directory / "catalog.json")
    for spec in registry["resources"]:
        for dimension, source in spec["dimensions"].items():
            for contract in source["contracts"]:
                matches = [
                    row
                    for group in groups
                    if group["key"] == spec["key"]
                    for row in group["rows"]
                    if row["dimension"] == dimension and row["source_type"] == contract["source_type"]
                ]
                if len(matches) == 1:
                    contracts.setdefault(contract["id"], matches[0])
    if store:
        with store.transaction() as connection:
            task_rows = list(connection.execute("SELECT * FROM tasks ORDER BY rowid"))
            tasks = {row["id"]: json.loads(row["spec"]) for row in task_rows}
            verdicts = list(connection.execute("SELECT * FROM verdicts ORDER BY id"))
            owners = list(connection.execute("SELECT * FROM owned"))
            state_root = Path(connection.execute("PRAGMA database_list").fetchone()[2]).parent
        # An untouched contract still belongs in the complete results table.
        # Materialize frozen task contracts without inventing test observations
        # or borrowing a PASS from another source/branch.
        for spec in tasks.values():
            for dimension, source in spec["dimensions"].items():
                for contract in source["contracts"]:
                    if contract["id"] in contracts:
                        continue
                    if "retest_scope" in spec and not any(
                        f"{contract['id']}-{mode}" in spec["retest_scope"]["scene_ids"] for mode in ("iframe", "open")
                    ):
                        # A failure-only plan must not create new report rows for
                        # unselected catalog dimensions; their old coverage is unchanged.
                        continue
                    group = next((group for group in groups if group["key"] == spec["key"]), None)
                    if not group:
                        group = {"key": spec["key"], "service": spec["service"], "name": spec["name"], "rows": []}
                        groups.append(group)
                    row = {
                        "contract_id": contract["id"], "dimension": dimension,
                        "source_type": contract["source_type"],
                        "status": {"iframe": "untested", "open": "untested"},
                        "reason": "当前计划尚无此入口的实际验收结果；未测不代表无资源或不支持。",
                    }
                    group["rows"].append(row)
                    contracts[contract["id"]] = row
        for verdict in verdicts:
            scene = verdict["scene"]
            spec = tasks[verdict["task_id"]]
            item = json.loads(verdict["receipt"])
            mode = scene.rsplit("-", 1)[-1]
            if mode == "creation":
                dimension = scene.rsplit("-", 2)[-2]
                source = spec["dimensions"].get(dimension)
                if not source or scene != f"{spec['id']}-{dimension}-creation" or dimension == "QUERY":
                    continue
                links = []
                for field, label in (("evidence", "创建记录"), ("source_evidence", "原始来源")):
                    if isinstance(item.get(field), str):
                        links.append(evidence_link(state_root, destination, item[field], label))
                if isinstance(item.get("evidence"), str):
                    try:
                        evidence = read_json(contained(state_root, item["evidence"]))
                        if isinstance(evidence.get("screenshot"), str):
                            links.append(evidence_link(state_root, destination, evidence["screenshot"], "截图"))
                    except (OSError, ValueError, AttributeError):
                        pass
                for contract in source["contracts"]:
                    row = contracts[contract["id"]]
                    if row.get("creation", {}).get("status") == "pass" and verdict["status"] != "pass":
                        continue  # An accepted creation checkpoint, like a click PASS, remains retained.
                    row["creation"] = {
                        "status": verdict["status"], "label": STATUS[verdict["status"]],
                        "reason": item["reason"], "links": links,
                        "managed": dimension == "TF" and source.get("execution") == "ros_managed_terraform",
                    }
                continue
            if mode not in {"iframe", "open"}:
                continue
            contract_id = scene.rsplit("-", 1)[0]
            if contract_id not in contracts:
                source = next(
                    (
                        (dim, contract)
                        for dim, source in spec["dimensions"].items()
                        for contract in source["contracts"]
                        if contract["id"] == contract_id
                    ),
                    None,
                )
                if not source:
                    continue
                group = next((group for group in groups if group["key"] == spec["key"]), None)
                if not group:
                    group = {"key": spec["key"], "service": spec["service"], "name": spec["name"], "rows": []}
                    groups.append(group)
                row = {
                    "contract_id": contract_id,
                    "dimension": source[0],
                    "source_type": source[1]["source_type"],
                    "status": {"iframe": "untested", "open": "untested"},
                    "reason": "",
                }
                group["rows"].append(row)
                contracts[contract_id] = row
            row = contracts[contract_id]
            if row["status"][mode] == "pass" and verdict["status"] != "pass":
                raise WorkflowError("cannot overwrite historical PASS with an unverified downgrade")
            row["status"][mode] = verdict["status"]
            if (
                row["dimension"] == "TF"
                and spec["dimensions"]["TF"].get("execution") == "ros_managed_terraform"
                and verdict["status"] in {"pass", "fail", "unsupported"}
                and item.get("screenshot") and item.get("source_evidence")
            ):
                row.setdefault("ros_managed_modes", []).append(mode)
            row.setdefault("latest_reasons", {})[mode] = item["reason"]
        cleanup_owners: dict[str, list[Any]] = {}
        for owner in owners:
            spec = tasks.get(owner["task_id"])
            if not spec:
                continue
            for contract in spec["dimensions"].get(owner["dimension"], {}).get("contracts", []):
                cleanup_owners.setdefault(contract["id"], []).append(owner)
        for contract_id, values in cleanup_owners.items():
            contracts[contract_id]["cleanup"] = cleanup_summary(state_root, values, destination)
    for group in groups:
        for row in group["rows"]:
            if row["dimension"] == "QUERY":
                # Query navigation may use an approved temporary sample graph.
                # Keep its actual ownership/cleanup evidence without crediting
                # the graph as a separate CREATE/ROS/TF test result.
                if "cleanup" in row:
                    row["creation"] = {"status": "untested", "label": "不适用 · 查询导航"}
                    row["cleanup"]["note"] = (
                        "查询导航使用临时样本；清理状态覆盖其测试依赖，不代表创建维度通过。"
                        + row["cleanup"].get("note", "")
                    )
                else:
                    row["creation"] = {"status": "untested", "label": "不适用 · 只读"}
                    row["cleanup"] = {"status": "untested", "label": "不适用 · 只读"}
            if row.get("latest_reasons"):
                row["reason"] = "；".join(
                    ("iframe" if mode == "iframe" else "直接打开") + "：" + reason
                    for mode, reason in row["latest_reasons"].items()
                )
    if store:
        # A preparation/source blocker has no new verdict. Explain the latest
        # frozen pending branch without rewriting its historical test outcome
        # or leaking an older plan's blocker into a subsequent plan.
        latest_tasks = {task["resource_id"]: task for task in task_rows}
        for task in latest_tasks.values():
            if task["state"] != "blocked" or not task["reason"]:
                continue
            scope = tasks[task["id"]].get("retest_scope", {})
            if scope.get("kind") not in {
                "failed_navigation_existing", "failed_query_navigation", "failed_navigation_managed",
            }:
                continue
            for scene in scope.get("scene_ids", []):
                contract_id, _, mode = scene.rpartition("-")
                row = contracts.get(contract_id)
                if mode not in {"iframe", "open"} or not row or row["status"][mode] in {"pass", "unsupported"}:
                    continue
                label = "iframe" if mode == "iframe" else "直接打开"
                current = "【当前复测阻塞｜{}｜{}】{}（原测试结论保留；阻塞说明不改变验收状态。）".format(
                    task["run_id"], label, task["reason"],
                )
                row["reason"] = row["reason"] + "；" + current if row["reason"] else current
    atomic_write(destination, document("资源链接验收结果", groups))
