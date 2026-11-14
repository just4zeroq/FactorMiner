# -*- coding: utf-8 -*-
"""把 docs/book/ 下的分章 Markdown 构建为：
   1) 合订全本 Markdown
   2) 单文件 HTML 电子书（侧边目录 / 搜索 / 打印友好 / 浅色主题）

无第三方依赖，可重复运行：python docs/book/_build.py
"""
from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_MD = ROOT / "因子挖掘_全本.md"
OUT_HTML = ROOT / "index.html"

CHAPTERS = [ROOT / f"ch{i:02d}.md" for i in range(1, 21)]
SOURCES = [ROOT / "front.md", *CHAPTERS, ROOT / "appendix.md"]

PARTS = [
    ("序", (0, 0)),
    ("第一篇 · 基础：因子到底在预测什么", (1, 3)),
    ("第二篇 · 算法与范式：候选从哪里来", (4, 10)),
    ("第三篇 · 因子挖掘全流程：从假设到可交易组合", (11, 16)),
    ("第四篇 · 工程实践与行业参照：把研究变成可复现系统", (17, 20)),
    ("附录", (21, 21)),
]


# ---------------------------------------------------------------- Markdown -> HTML
def slug(text: str, used: set[str]) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text, flags=re.UNICODE).strip("-").lower()
    s = s or "section"
    base, i = s, 2
    while s in used:
        s = f"{base}-{i}"
        i += 1
    used.add(s)
    return s


def inline(text: str) -> str:
    """行内标记；先占位行内代码，避免内部被当作标记。"""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00CODE{len(codes) - 1}\x00"

    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = text.replace("\\|", "|")
    for i, c in enumerate(codes):
        text = text.replace(f"\x00CODE{i}\x00", c)
    return text


def is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|[\s:\-|]+\|\s*$", line)) and "-" in line


def split_row(line: str) -> list[str]:
    """切分表格行；行内代码中的 `|` 与转义的 \\| 不参与分列。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    stash: list[str] = []

    def keep(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00P{len(stash) - 1}\x00"

    s = re.sub(r"\\\|", keep, s)          # 显式转义的竖线
    s = re.sub(r"`[^`]*`", keep, s)       # 行内代码（常含 |x|、|corr| 等）
    cells = [c.strip() for c in s.split("|")]
    for i, v in enumerate(stash):
        cells = [c.replace(f"\x00P{i}\x00", v) for c in cells]
    return cells


def render(md: str, used_ids: set[str], toc: list[dict]) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]

        # 代码块
        if line.lstrip().startswith("```"):
            lang = line.strip()[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            hid = slug(re.sub(r"[*`]", "", title), used_ids)
            out.append(f'<h{level} id="{hid}">{inline(title)}</h{level}>')
            if level in (2, 3):
                toc.append({"level": level, "title": re.sub(r"[*`]", "", title), "id": hid})
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # 表格
        if line.lstrip().startswith("|") and i + 1 < n and is_table_sep(lines[i + 1]):
            head = split_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            body = []
            for r in rows:
                cells = (r + [""] * len(head))[: len(head)]
                body.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            out.append(
                '<div class="table-wrap"><table><thead><tr>'
                + th
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
            continue

        # 引用块
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = render("\n".join(buf), used_ids, [])
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # 列表
        if re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items: list[str] = []
            while i < n and re.match(r"^\s*(?:[-*+]|\d+\.)\s+", lines[i]):
                item = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", lines[i])
                item = re.sub(r"^\[([ xX])\]\s*", lambda mm: "☑ " if mm.group(1).lower() == "x" else "☐ ", item)
                # 收集续行（缩进）
                i += 1
                while i < n and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{inline(item)}</li>")
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 段落
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|```|\||>|(?:\s*)(?:[-*+]|\d+\.)\s|(?:\s*)(?:---+|\*\*\*+)\s*$)", lines[i]
        ):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + inline(" ".join(x.strip() for x in buf)) + "</p>")

    return "\n".join(out)


# ---------------------------------------------------------------- 构建
def build() -> None:
    for p in SOURCES:
        if not p.exists():
            raise SystemExit(f"缺少章节文件：{p}")

    # 1) 合订 Markdown
    front = (ROOT / "front.md").read_text(encoding="utf-8").rstrip()
    parts_md = [front, "\n\n---\n"]
    for idx, p in enumerate(CHAPTERS + [ROOT / "appendix.md"]):
        parts_md.append(p.read_text(encoding="utf-8").rstrip())
        parts_md.append("\n\n---\n")
    OUT_MD.write_text("\n".join(parts_md).rstrip() + "\n", encoding="utf-8")

    # 2) HTML：给每个文件包一个 section，便于侧栏定位
    used: set[str] = set()
    toc_all: list[dict] = []
    sections: list[str] = []
    files = [ROOT / "front.md", *CHAPTERS, ROOT / "appendix.md"]
    for n, p in enumerate(files):
        md = p.read_text(encoding="utf-8")
        local_toc: list[dict] = []
        body = render(md, used, local_toc)
        sid = f"chapter-{n}"
        sections.append(f'<section class="chapter" id="{sid}" data-title="{html.escape(p.stem)}">{body}</section>')
        for t in local_toc:
            t["section"] = sid
            toc_all.append(t)

    # 侧栏目录：H2 为主，H3 折叠在 H2 下
    nav: list[str] = []
    cur_part = None
    for n, p in enumerate(files):
        part = next((name for name, (a, b) in PARTS if a <= n <= b), "其它")
        if part != cur_part:
            if cur_part is not None:
                nav.append("</div>")
            nav.append(f'<div class="nav-group"><div class="nav-group-title">{html.escape(part)}</div>')
            cur_part = part
        title = ""
        for line in p.read_text(encoding="utf-8").splitlines()[:4]:
            if line.startswith("# "):
                title = line[2:].strip()
                break
            if line.startswith("## 第"):
                title = line[3:].strip()
                break
        nav.append(f'<a class="nav-item" href="#chapter-{n}">{html.escape(title or p.stem)}</a>')
    if cur_part is not None:
        nav.append("</div>")

    html_doc = HTML_SHELL.replace("{{NAV}}", "\n".join(nav))
    html_doc = html_doc.replace("{{CONTENT}}", "\n".join(sections))
    OUT_HTML.write_text(html_doc, encoding="utf-8")

    print(f"全本 Markdown : {OUT_MD}  ({OUT_MD.stat().st_size:,} bytes)")
    print(f"HTML 电子书   : {OUT_HTML}  ({OUT_HTML.stat().st_size:,} bytes)")
    print(f"章节数        : {len(files)}（前言 + {len(CHAPTERS)} 章 + 附录）")
    print(f"目录条目      : {len(toc_all)}")


HTML_SHELL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>因子挖掘：概念、算法、原理与工程实践</title>
<style>
:root{
  --bg:#fbfbfd; --panel:#ffffff; --sidebar:#f4f6f9; --line:#e3e7ee;
  --ink:#1f2430; --ink-soft:#4a5464; --muted:#7c8798; --accent:#2b5cd9;
  --accent-soft:#eaf0ff; --code-bg:#f4f6f9; --table-head:#f2f5fa;
  --mark:#fff2a8; --shadow:0 1px 3px rgba(24,38,66,.07);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:16px;line-height:1.85;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* 顶栏 */
.topbar{position:fixed;top:0;left:0;right:0;height:56px;background:rgba(255,255,255,.94);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line);z-index:50;
  display:flex;align-items:center;gap:16px;padding:0 20px}
.topbar .brand{font-weight:700;font-size:15px;letter-spacing:.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.topbar .brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:10px}
.topbar .spacer{flex:1}
.search{position:relative}
.search input{width:230px;height:32px;border:1px solid var(--line);border-radius:8px;
  padding:0 10px 0 30px;font-size:13px;background:var(--panel);color:var(--ink);outline:none}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search svg{position:absolute;left:9px;top:8px;opacity:.45}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink-soft);
  border-radius:8px;height:32px;padding:0 11px;font-size:12.5px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent)}
#menuToggle{display:none}

/* 侧栏 */
.sidebar{position:fixed;top:56px;bottom:0;left:0;width:286px;overflow-y:auto;
  background:var(--sidebar);border-right:1px solid var(--line);padding:18px 12px 60px;z-index:40}
.nav-group{margin-bottom:18px}
.nav-group-title{font-size:11px;font-weight:700;letter-spacing:.9px;color:var(--muted);
  text-transform:uppercase;padding:6px 10px 8px;border-bottom:1px solid var(--line);margin-bottom:6px}
.nav-item{display:block;padding:6px 10px;border-radius:7px;color:var(--ink-soft);
  font-size:13px;line-height:1.5;margin:1px 0}
.nav-item:hover{background:#e9edf5;text-decoration:none}
.nav-item.active{background:var(--accent-soft);color:var(--accent);font-weight:600}
.nav-item.hidden{display:none}

/* 正文 */
.main{margin-left:286px;padding:88px 0 120px}
.chapter{max-width:860px;margin:0 auto;padding:38px 46px 46px;background:var(--panel);
  border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);margin-bottom:38px}
.chapter:first-child{padding-top:46px}
h1{font-size:30px;line-height:1.4;margin:0 0 18px;letter-spacing:.3px}
h2{font-size:24px;line-height:1.45;margin:44px 0 18px;padding-bottom:10px;border-bottom:2px solid var(--line)}
h3{font-size:18.5px;line-height:1.5;margin:34px 0 12px;color:#20293b}
h4{font-size:16px;margin:24px 0 10px;color:var(--ink-soft)}
p{margin:0 0 15px;color:var(--ink)}
blockquote{margin:18px 0;padding:12px 18px;background:#f6f8fd;border-left:4px solid var(--accent);
  border-radius:0 8px 8px 0;color:var(--ink-soft)}
blockquote p{margin:0 0 8px}
blockquote p:last-child{margin:0}
hr{border:0;border-top:1px solid var(--line);margin:36px 0}
ul,ol{margin:0 0 16px;padding-left:26px}
li{margin:5px 0}
li::marker{color:var(--accent)}
strong{color:#161c28;font-weight:650}
code{background:var(--code-bg);border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;font-size:.88em;font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace;
  color:#a3345c;word-break:break-word}
pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;overflow-x:auto;margin:18px 0;line-height:1.65}
pre code{background:none;border:0;padding:0;color:#26303f;font-size:12.8px;white-space:pre}
.table-wrap{overflow-x:auto;margin:18px 0}
table{border-collapse:collapse;width:100%;font-size:13.6px;line-height:1.65}
th,td{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
th{background:var(--table-head);font-weight:650;color:#26303f;white-space:nowrap}
tbody tr:nth-child(even){background:#fafbfd}
td code{font-size:12px}
mark{background:var(--mark);padding:0 2px;border-radius:2px}

/* 章导航 */
.chapter-nav{display:flex;justify-content:space-between;gap:14px;margin-top:44px;
  padding-top:20px;border-top:1px solid var(--line);font-size:13px}
.chapter-nav a{background:var(--accent-soft);padding:8px 14px;border-radius:8px}
.footer{max-width:860px;margin:0 auto;color:var(--muted);font-size:12.5px;text-align:center;padding:0 20px}
.backtop{position:fixed;right:24px;bottom:24px;z-index:30;display:none}

@media(max-width:1000px){
  .sidebar{transform:translateX(-100%);transition:transform .22s;box-shadow:6px 0 24px rgba(0,0,0,.08)}
  .sidebar.open{transform:translateX(0)}
  .main{margin-left:0;padding:76px 14px 90px}
  .chapter{padding:26px 22px 32px;border-radius:10px}
  #menuToggle{display:inline-block}
  .topbar .brand span{display:none}
  .search input{width:150px}
}
@media print{
  .topbar,.sidebar,.backtop,.chapter-nav{display:none!important}
  .main{margin:0;padding:0}
  .chapter{border:0;box-shadow:none;max-width:none;padding:0;margin:0;page-break-after:always}
  h2{page-break-after:avoid} h3{page-break-after:avoid}
  pre,table,blockquote{page-break-inside:avoid}
  body{font-size:11.5pt;background:#fff}
}
</style>
</head>
<body>
<div class="topbar">
  <button class="btn" id="menuToggle">目录</button>
  <div class="brand">因子挖掘：概念、算法、原理与工程实践<span>从信号假设到可证伪的 Alpha</span></div>
  <div class="spacer"></div>
  <div class="search">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
    <input id="search" placeholder="搜索章节 / 关键词…" autocomplete="off">
  </div>
  <button class="btn" id="fontBtn">字号</button>
  <button class="btn" onclick="window.print()">打印 / PDF</button>
</div>
<aside class="sidebar" id="sidebar">{{NAV}}</aside>
<main class="main" id="main">
{{CONTENT}}
<div class="footer">《因子挖掘：概念、算法、原理与工程实践》 · 基于 FactorMiner V4 与 20+ 开源框架的源码级对照 · 本书不提供投资建议</div>
</main>
<button class="btn backtop" id="backtop">↑ 顶部</button>
<script>
(function(){
  var sidebar=document.getElementById('sidebar'),
      main=document.getElementById('main'),
      toggle=document.getElementById('menuToggle'),
      search=document.getElementById('search'),
      backtop=document.getElementById('backtop'),
      items=[].slice.call(document.querySelectorAll('.nav-item')),
      chapters=[].slice.call(document.querySelectorAll('.chapter'));

  toggle.addEventListener('click',function(){sidebar.classList.toggle('open')});
  document.addEventListener('click',function(e){
    if(window.innerWidth<=1000 && sidebar.classList.contains('open') &&
       !sidebar.contains(e.target) && e.target!==toggle) sidebar.classList.remove('open');
  });

  // 每章末尾加上一章/下一章
  chapters.forEach(function(ch,i){
    var nav=document.createElement('div');nav.className='chapter-nav';
    if(i>0){var a=document.createElement('a');a.href='#'+chapters[i-1].id;a.textContent='← '+items[i-1].textContent;nav.appendChild(a);}
    else{var s=document.createElement('span');nav.appendChild(s);}
    if(i<chapters.length-1){var b=document.createElement('a');b.href='#'+chapters[i+1].id;b.textContent=items[i+1].textContent+' →';nav.appendChild(b);}
    ch.appendChild(nav);
  });

  // 滚动高亮
  function onScroll(){
    var y=window.scrollY+120,idx=0;
    chapters.forEach(function(c,i){if(c.offsetTop<=y)idx=i});
    items.forEach(function(a,i){a.classList.toggle('active',i===idx)});
    backtop.style.display=window.scrollY>500?'block':'none';
    var act=items[idx];if(act&&window.innerWidth>1000){
      var r=act.getBoundingClientRect();
      if(r.top<70||r.bottom>window.innerHeight-30) act.scrollIntoView({block:'center'});
    }
  }
  window.addEventListener('scroll',onScroll,{passive:true});onScroll();
  backtop.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'})});

  // 搜索：过滤侧栏 + 正文高亮
  var marks=[];
  function clearMarks(){marks.forEach(function(m){m.outerHTML=m.textContent});marks=[]}
  function doSearch(q){
    clearMarks();q=q.trim();
    items.forEach(function(a){
      a.classList.toggle('hidden',!!q && a.textContent.toLowerCase().indexOf(q.toLowerCase())<0);
    });
    if(q.length<2)return;
    var re=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi'),
        walker=document.createTreeWalker(main,NodeFilter.SHOW_TEXT),
        nodes=[],node;
    while(node=walker.nextNode()){
      if(node.parentNode.matches('script,style,code'))continue;
      if(re.test(node.nodeValue))nodes.push(node);
      re.lastIndex=0;
    }
    nodes.slice(0,400).forEach(function(nd){
      var span=document.createElement('span');
      span.innerHTML=nd.nodeValue.replace(re,function(s){return '<mark>'+s+'</mark>'});
      nd.parentNode.replaceChild(span,nd);
      marks=marks.concat([].slice.call(span.querySelectorAll('mark')));
    });
  }
  var timer;search.addEventListener('input',function(){
    clearTimeout(timer);var v=search.value;timer=setTimeout(function(){doSearch(v)},220);
  });
  search.addEventListener('keydown',function(e){if(e.key==='Escape'){search.value='';doSearch('')}});

  // 字号
  var sizes=[16,17.2,18.4],si=0;
  document.getElementById('fontBtn').addEventListener('click',function(){
    si=(si+1)%sizes.length;document.body.style.fontSize=sizes[si]+'px';
    this.textContent='字号 '+(si+1);
  });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build()
