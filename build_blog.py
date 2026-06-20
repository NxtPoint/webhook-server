# build_blog.py — Minimal, dependency-free static blog generator for the
# public marketing site. Converts Markdown posts in frontend/blog/_posts/*.md
# into native, SEO-ready HTML at frontend/blog/<slug>.html, plus a blog index
# at frontend/blog/index.html.
#
# Why this exists: the marketing site (marketing_app.py) serves native HTML so
# Google reads the full article — no Wix JS, no iframe. Each post gets Article +
# BreadcrumbList JSON-LD, Open Graph cards, a canonical at /post/<slug>, and
# internal links to /overview, /pricing, /coaching. marketing_app.sitemap_xml()
# auto-includes every generated post.
#
# Workflow for a new weekly post:
#   1) Drop a file in frontend/blog/_posts/<slug>.md with frontmatter:
#         ---
#         title: My Post Title
#         description: One-line meta description (~150 chars).
#         date: 2026-06-15
#         ---
#         Body in Markdown (## headings, lists, **bold**, [links](url), tables)...
#   2) Run:  .venv/Scripts/python build_blog.py
#   3) Commit the generated frontend/blog/*.html (and push).
#
# Supported Markdown: ##/### headings, paragraphs, - / * bullet lists,
# **bold**, [text](url) links, and GFM pipe tables. (Deliberately minimal.)

import os
import re
import glob
import html as _html

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLOG_DIR = os.path.join(BASE_DIR, "frontend", "blog")
POSTS_DIR = os.path.join(BLOG_DIR, "_posts")

SITE = "https://www.ten-fifty5.com"
OG_IMAGE = f"{SITE}/og/og_homepage.png"

# ---- Markdown → HTML (minimal) ------------------------------------------------

def _inline(text):
    """Inline formatting: escape, then bold + links. Order matters."""
    out = _html.escape(text, quote=False)
    # links [text](url) — apply before bold so bracketed text is safe
    out = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'<a href="\2">\1</a>', out)
    # bold **text** (must run before single-* italics)
    out = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', out)
    # italic *text* (single asterisks left after bold)
    out = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', out)
    return out


def _table(rows):
    """rows: list of raw '| a | b |' lines (incl. header + separator)."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header = cells[0]
    body = cells[2:]  # cells[1] is the --- separator
    out = ['<div class="table-wrap"><table>', "<thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def md_to_html(md):
    lines = md.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        # Heading
        m = re.match(r'^(#{2,4})\s+(.*)$', s)
        if m:
            level = len(m.group(1))
            blocks.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        # Table (line with pipes followed by a separator row)
        if s.startswith("|") and i + 1 < len(lines) and re.match(r'^\|[\s:\-|]+\|?$', lines[i+1].strip()):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            blocks.append(_table(tbl))
            continue
        # List
        if re.match(r'^[-*]\s+', s):
            items = []
            while i < len(lines) and re.match(r'^[-*]\s+', lines[i].strip()):
                items.append(f"<li>{_inline(re.sub(r'^[-*]\s+', '', lines[i].strip()))}</li>")
                i += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue
        # Paragraph (gather until blank)
        para = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'^(#{2,4}\s|[-*]\s|\|)', lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        blocks.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(blocks)


# ---- Frontmatter --------------------------------------------------------------

def parse_post(path):
    raw = open(path, encoding="utf-8").read()
    meta, body = {}, raw
    if raw.startswith("---"):
        _, fm, body = raw.split("---", 2)
        for ln in fm.strip().split("\n"):
            if ":" in ln:
                k, v = ln.split(":", 1)
                val = v.strip()
                # strip a single layer of matching wrapping quotes (so a
                # quoted YAML title doesn't render literal " in the heading)
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1].strip()
                meta[k.strip()] = val
    meta["slug"] = os.path.splitext(os.path.basename(path))[0]
    meta["body_html"] = md_to_html(body.strip())
    return meta


# ---- Templates ----------------------------------------------------------------

STYLE = """
:root{--bg:#f5f5f5;--white:#fff;--green:#1a5c2e;--green-light:#22783c;--green-dark:#134221;
--green-bg:rgba(26,92,46,0.08);--text:#1a1a1a;--text-sec:#6b7280;--text-dim:#9ca3af;--border:#e5e5e5;--radius:4px}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:focus-visible{outline:2px solid var(--green);outline-offset:2px}
html{font-size:16px;scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--white);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--green);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:820px;margin:0 auto;padding:0 28px}
.eyebrow{display:inline-flex;align-items:center;gap:10px;font-size:.72rem;font-weight:600;color:var(--green);text-transform:uppercase;letter-spacing:.14em}
.eyebrow::before{content:"";width:20px;height:1px;background:var(--green);opacity:.6}
.post-head{padding:80px 0 30px;background:linear-gradient(180deg,var(--green-bg),transparent)}
.post-head h1{font-size:clamp(2rem,4.6vw,3rem);font-weight:800;letter-spacing:-.02em;line-height:1.08;color:var(--text);margin-top:16px}
.post-meta{margin-top:18px;color:var(--text-sec);font-size:.9rem}
.post-hero{max-width:820px;margin:0 auto;padding:26px 28px 0}
.post-hero img{width:100%;border-radius:12px;border:1px solid var(--border);box-shadow:0 18px 40px -24px rgba(10,31,20,.35);display:block}
.post-hero figcaption{margin-top:10px;font-size:.82rem;color:var(--text-dim);text-align:center}
.article{padding:34px 0 70px}
.article h2{font-size:1.55rem;font-weight:700;letter-spacing:-.01em;margin:42px 0 14px;color:var(--text)}
.article h3{font-size:1.2rem;font-weight:700;margin:30px 0 10px;color:var(--text)}
.article h4{font-size:1.02rem;font-weight:700;margin:24px 0 8px;color:var(--text)}
.article p{margin:0 0 18px;color:#27303a;font-size:1.05rem}
.article ul{margin:0 0 20px;padding-left:24px}
.article li{margin:0 0 9px;color:#27303a;font-size:1.05rem}
.article strong{color:var(--text);font-weight:700}
.table-wrap{overflow-x:auto;margin:0 0 24px}
.article table{border-collapse:collapse;width:100%;font-size:.95rem}
.article th,.article td{border:1px solid var(--border);padding:10px 12px;text-align:left;vertical-align:top}
.article th{background:var(--green-bg);font-weight:700;color:var(--text)}
.cta-band{margin:44px 0 0;padding:34px 30px;background:var(--green-dark);border-radius:12px;color:#fff;text-align:center}
.cta-band h3{color:#fff;font-size:1.3rem;margin-bottom:10px}
.cta-band p{color:rgba(255,255,255,.75);margin-bottom:20px}
.cta-band a.btn{display:inline-block;background:#fff;color:var(--green);padding:12px 28px;border-radius:var(--radius);font-weight:700}
.backlink{display:inline-block;margin-top:30px;color:var(--green);font-weight:600}
/* index */
.idx-head{padding:84px 0 24px;background:linear-gradient(180deg,var(--green-bg),transparent)}
.idx-head h1{font-size:clamp(2.2rem,5vw,3.2rem);font-weight:800;letter-spacing:-.02em;margin-top:16px}
.idx-head p{margin-top:14px;color:var(--text-sec);font-size:1.1rem;max-width:560px}
.post-list{padding:30px 0 70px;display:grid;gap:6px}
.post-card{display:grid;grid-template-columns:230px 1fr;gap:26px;align-items:center;padding:24px 0;border-bottom:1px solid var(--border)}
.post-card:hover{text-decoration:none}
.post-card .thumb{aspect-ratio:16/10;border-radius:10px;overflow:hidden;background:linear-gradient(150deg,#226e3c,#0c3a1e);border:1px solid var(--border)}
.post-card .thumb img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .35s ease}
.post-card:hover .thumb img{transform:scale(1.04)}
.post-card .date{color:var(--text-sec);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em}
.post-card h2{font-size:1.35rem;font-weight:700;letter-spacing:-.01em;color:var(--text);margin:7px 0 8px}
.post-card:hover h2{color:var(--green)}
.post-card p{color:var(--text-sec);font-size:.98rem}
@media(max-width:600px){.post-card{grid-template-columns:1fr;gap:14px}}
/* footer */
.footer{background:var(--green-dark);color:rgba(255,255,255,.82);padding:60px 0 28px;margin-top:20px}
.footer-inner{max-width:1100px;margin:0 auto;padding:0 28px;display:grid;grid-template-columns:2fr 1fr 1fr;gap:40px}
.footer-brand-name{font-size:1.2rem;font-weight:800;color:#fff}
.footer-brand p{margin-top:12px;font-size:.9rem;color:rgba(255,255,255,.6);max-width:360px}
.footer-col h5{color:#fff;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;margin-bottom:14px}
.footer-col ul{list-style:none}.footer-col li{margin-bottom:9px;font-size:.92rem}
.footer-col a{color:rgba(255,255,255,.82)}.footer-col a:hover{color:#fff}
.footer-bottom{max-width:1100px;margin:36px auto 0;padding:22px 28px 0;border-top:1px solid rgba(255,255,255,.12);display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:.82rem;color:rgba(255,255,255,.5)}
@media(max-width:720px){.footer-inner{grid-template-columns:1fr;gap:28px}}
/* shared top nav — centered links, matches the rest of the marketing site */
.topnav{position:sticky;top:0;z-index:1000;background:rgba(255,255,255,0.96);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.topnav-inner{max-width:1200px;margin:0 auto;padding:0 28px;height:62px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:18px}
.topnav-logo{justify-self:start;font-weight:800;font-size:1.1rem;letter-spacing:-.01em;color:var(--text);text-decoration:none;white-space:nowrap}
.topnav-logo:hover{text-decoration:none}
.topnav-links{display:flex;justify-content:center;align-items:center;gap:22px}
.topnav-links a{font-size:.92rem;font-weight:500;color:var(--text-sec);text-decoration:none;transition:color .2s;white-space:nowrap}
.topnav-links a:hover{color:var(--green);text-decoration:none}
.topnav-links a.active{color:var(--green);font-weight:600}
.skip-link{position:absolute;left:-9999px;top:0;z-index:2000;background:var(--green);color:#fff;padding:10px 16px;border-radius:4px;font-weight:600;text-decoration:none}
.skip-link:focus{left:8px;top:8px}
.topnav-right{justify-self:end;display:flex;align-items:center;gap:10px}
.topnav-cta{background:var(--green);color:#fff!important;padding:9px 18px;border-radius:4px;font-weight:600!important;white-space:nowrap}
.topnav-cta:hover{background:var(--green-light);text-decoration:none}
.topnav-toggle{display:none;font-size:1.5rem;line-height:1;background:none;border:none;color:var(--text);cursor:pointer;padding:6px}
@media(max-width:980px){
.topnav-inner{display:flex;justify-content:space-between}
.topnav-toggle{display:block}
.topnav-links{display:none;position:absolute;top:62px;left:0;right:0;background:#fff;flex-direction:column;align-items:stretch;justify-content:flex-start;gap:0;padding:6px 0;border-bottom:1px solid var(--border);box-shadow:0 8px 20px rgba(0,0,0,.06)}
.topnav-links.open{display:flex}
.topnav-links a{padding:13px 28px}
}
"""

# highlight the current page in the shared nav (Blog stays active on /post/*)
NAV_ACTIVE_JS = ('<script>(function(){try{var p=(location.pathname||"/").replace(/\\/+$/,"")||"/";'
                 'document.querySelectorAll(".topnav-links a").forEach(function(a){'
                 'var ap=new URL(a.href).pathname.replace(/\\/+$/,"")||"/";'
                 'if(ap===p||(ap==="/blog"&&p.indexOf("/post")===0))a.classList.add("active");});}catch(e){}})();</script>')

NAV = f"""<nav class="topnav">
  <div class="topnav-inner">
    <a href="{SITE}/" class="topnav-logo">TEN-FIFTY5</a>
    <div class="topnav-links">
      <a href="{SITE}/">Home</a>
      <a href="{SITE}/overview">How It Works</a>
      <a href="{SITE}/pricing">Pricing</a>
      <a href="{SITE}/coaching">For Coaches</a>
      <a href="{SITE}/academies">Academies</a>
      <a href="{SITE}/blog">Blog</a>
      <a href="{SITE}/contact-us">Contact</a>
    </div>
    <div class="topnav-right">
      <a href="{SITE}/login" class="topnav-cta">Start Free</a>
      <button class="topnav-toggle" aria-label="Toggle menu" onclick="document.querySelector('.topnav-links').classList.toggle('open')">&#9776;</button>
    </div>
  </div>
</nav>
""" + NAV_ACTIVE_JS

FOOTER = f"""<footer class="footer">
  <div class="footer-inner">
    <div class="footer-brand">
      <div class="footer-brand-name">TEN-FIFTY5</div>
      <p>ATP-level match analysis, biomechanical technique breakdown and AI coaching — for serious competitive players and their coaches.</p>
    </div>
    <div class="footer-col"><h5>Product</h5><ul>
      <li><a href="{SITE}/" target="_top">Home</a></li>
      <li><a href="{SITE}/overview" target="_top">How It Works</a></li>
      <li><a href="{SITE}/pricing" target="_top">Pricing</a></li>
      <li><a href="{SITE}/coaching" target="_top">For Coaches</a></li>
      <li><a href="{SITE}/academies" target="_top">For Academies</a></li>
      <li><a href="{SITE}/blog" target="_top">Blog</a></li>
    </ul></div>
    <div class="footer-col"><h5>Get in touch</h5><ul>
      <li><a href="mailto:info@ten-fifty5.com">info@ten-fifty5.com</a></li>
      <li><a href="{SITE}/contact-us" target="_top">Contact</a></li>
      <li><a href="{SITE}/privacy-policy" target="_top">Privacy Policy</a></li>
      <li><a href="{SITE}/subprocessors" target="_top">Sub-processors</a></li>
      <li><a href="#" onclick="if(window.TFCookie){{TFCookie.open();}}return false;">Cookie settings</a></li>
    </ul></div>
  </div>
  <div class="footer-bottom"><span>&copy; 2026 Ten-Fifty5. All rights reserved.</span><span>Built for the serious player.</span></div>
</footer>"""

FONT = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">')

FAVICON = ('<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
           '<link rel="icon" href="/favicon.ico" sizes="any">'
           '<link rel="apple-touch-icon" href="/apple-touch-icon.png">')

# Cookie consent (prior opt-in, granular). Self-contained, identical to the snippet
# embedded in the static marketing pages (frontend/*.html). Gates the analytics
# beacon (analytics.js no-ops until the Analytics category is granted). Plain string
# (NOT an f-string) so the JS braces are emitted verbatim.
COOKIE_BANNER = r"""<!-- Cookie consent (prior opt-in, granular) -->
<style>
#tfck-bar{position:fixed;left:0;right:0;bottom:0;z-index:99998;background:#0a1814;color:rgba(255,255,255,.82);border-top:1px solid rgba(255,255,255,.12);box-shadow:0 -8px 30px rgba(0,0,0,.28);font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}
.tfck-inner{max-width:1200px;margin:0 auto;padding:16px 28px;display:flex;flex-wrap:wrap;align-items:center;gap:14px 24px;}
.tfck-copy{flex:1 1 320px;min-width:260px;font-size:.84rem;line-height:1.55;}
.tfck-copy strong{color:#fff;font-weight:700;}
.tfck-copy a{color:#86E8A4;text-decoration:underline;}
.tfck-cats{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;}
.tfck-cat{display:inline-flex;align-items:center;gap:7px;font-size:.82rem;color:rgba(255,255,255,.85);cursor:pointer;white-space:nowrap;}
.tfck-cat input{width:16px;height:16px;accent-color:#22783c;cursor:pointer;}
.tfck-cat.tfck-locked{color:rgba(255,255,255,.55);cursor:default;}
.tfck-cat.tfck-locked input{cursor:not-allowed;}
.tfck-cat.tfck-locked span{font-size:.72rem;color:rgba(255,255,255,.4);}
.tfck-btns{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto;}
.tfck-btn{padding:9px 16px;font-size:.82rem;font-weight:600;border-radius:4px;cursor:pointer;border:1px solid transparent;white-space:nowrap;transition:background .2s,border-color .2s,color .2s;}
.tfck-btn.tfck-primary{background:#1a5c2e;color:#fff;}
.tfck-btn.tfck-primary:hover{background:#22783c;}
.tfck-btn.tfck-ghost{background:transparent;color:#fff;border-color:rgba(255,255,255,.35);}
.tfck-btn.tfck-ghost:hover{background:rgba(255,255,255,.08);border-color:#fff;}
.tfck-btn:focus-visible{outline:2px solid #86E8A4;outline-offset:2px;}
@media(max-width:640px){.tfck-btns{margin-left:0;width:100%;}.tfck-btn{flex:1 1 auto;}}
</style>
<script>
(function () {
  "use strict";
  var KEY = "tf_cookie_consent";
  function read() {
    try { var r = localStorage.getItem(KEY); return r ? JSON.parse(r) : null; }
    catch (e) { return null; }
  }
  function write(analytics, marketing) {
    var c = { analytics: !!analytics, marketing: !!marketing, ts: Date.now(), v: 1 };
    try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {}
    try { window.dispatchEvent(new Event("tf-consent-changed")); } catch (e) {}
    return c;
  }
  var bar = null;
  function hide() { if (bar) bar.style.display = "none"; }
  function openPanel() {
    build();
    var c = read() || { analytics: false, marketing: false };
    var a = document.getElementById("tfck-analytics");
    var m = document.getElementById("tfck-marketing");
    if (a) a.checked = !!c.analytics;
    if (m) m.checked = !!c.marketing;
    if (bar) bar.style.display = "block";
  }
  function acceptAll() { write(true, true); hide(); }
  function rejectAll() { write(false, false); hide(); }
  function saveChoices() {
    var a = document.getElementById("tfck-analytics");
    var m = document.getElementById("tfck-marketing");
    write(a && a.checked, m && m.checked); hide();
  }
  function build() {
    if (bar) return;
    bar = document.createElement("div");
    bar.id = "tfck-bar";
    bar.setAttribute("role", "dialog");
    bar.setAttribute("aria-label", "Cookie consent");
    bar.innerHTML =
      '<div class="tfck-inner">' +
        '<div class="tfck-copy">' +
          '<strong>We respect your privacy.</strong> We use necessary cookies to run the site. ' +
          'With your permission we also use analytics and marketing cookies to improve Ten-Fifty5. ' +
          'Nothing non-essential runs until you choose. ' +
          '<a href="https://www.ten-fifty5.com/privacy-policy" target="_top">Privacy Policy</a>.' +
        '</div>' +
        '<div class="tfck-cats">' +
          '<label class="tfck-cat tfck-locked"><input type="checkbox" checked disabled> Necessary <span>(always on)</span></label>' +
          '<label class="tfck-cat"><input type="checkbox" id="tfck-analytics"> Analytics</label>' +
          '<label class="tfck-cat"><input type="checkbox" id="tfck-marketing"> Marketing</label>' +
        '</div>' +
        '<div class="tfck-btns">' +
          '<button type="button" class="tfck-btn tfck-ghost" id="tfck-reject">Reject all</button>' +
          '<button type="button" class="tfck-btn tfck-ghost" id="tfck-save">Save choices</button>' +
          '<button type="button" class="tfck-btn tfck-primary" id="tfck-accept">Accept all</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(bar);
    document.getElementById("tfck-accept").addEventListener("click", acceptAll);
    document.getElementById("tfck-reject").addEventListener("click", rejectAll);
    document.getElementById("tfck-save").addEventListener("click", saveChoices);
  }
  window.TFCookie = { open: openPanel, get: read };
  function init() { if (!read()) build(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
</script>
<!-- /Cookie consent -->"""

CTA_BAND = f"""<div class="cta-band">
  <h3>See your own game in data</h3>
  <p>Your first match is free — no credit card. Full dashboard, heatmaps, and AI coaching in a couple of hours.</p>
  <a class="btn" href="{SITE}/login" target="_top">Analyse my first match free</a>
</div>"""


def render_post(p):
    url = f"{SITE}/post/{p['slug']}"
    title = p.get("title", p["slug"])
    desc = p.get("description", "")
    date = p.get("date", "")
    image = p.get("image", "")
    hero = (f'<figure class="post-hero"><img src="{image}" alt="{_html.escape(title)}" '
            f'width="900" height="506" fetchpriority="high" decoding="async"></figure>\n') if image else ""
    og_img = f"{SITE}{image}" if image.startswith("/") else (image or OG_IMAGE)
    article = (
        '{"@context":"https://schema.org","@type":"Article",'
        f'"headline":{_json(title)},"description":{_json(desc)},'
        f'"datePublished":{_json(date)},"image":{_json(og_img)},'
        '"author":{"@type":"Organization","name":"Ten-Fifty5"},'
        '"publisher":{"@type":"Organization","name":"Ten-Fifty5","logo":{"@type":"ImageObject","url":'
        f'{_json(OG_IMAGE)}}}}},"mainEntityOfPage":{_json(url)}}}'
    )
    breadcrumb = (
        '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
        f'{{"@type":"ListItem","position":1,"name":"Blog","item":"{SITE}/blog"}},'
        f'{{"@type":"ListItem","position":2,"name":{_json(title)},"item":{_json(url)}}}]}}'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{_html.escape(title)} | Ten-Fifty5</title>
<meta name="description" content="{_html.escape(desc)}">
<link rel="canonical" href="{url}">
<meta name="robots" content="index, follow">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Ten-Fifty5">
<meta property="og:title" content="{_html.escape(title)}">
<meta property="og:description" content="{_html.escape(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{og_img}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_html.escape(title)}">
<meta name="twitter:description" content="{_html.escape(desc)}">
<meta name="twitter:image" content="{og_img}">
<script type="application/ld+json">{article}</script>
<script type="application/ld+json">{breadcrumb}</script>
{FAVICON}
{FONT}
<style>{STYLE}</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
{NAV}
<span id="main-content" tabindex="-1"></span>
<header class="post-head">
  <div class="wrap">
    <a class="eyebrow" href="{SITE}/blog" style="text-decoration:none;">Ten-Fifty5 Blog</a>
    <h1>{_html.escape(title)}</h1>
    <div class="post-meta">{_fmt_date(date)}</div>
  </div>
</header>
{hero}<main class="article">
  <div class="wrap">
{p['body_html']}
{CTA_BAND}
    <a class="backlink" href="{SITE}/blog">&larr; All articles</a>
  </div>
</main>
{FOOTER}
{COOKIE_BANNER}
</body>
</html>"""


def render_index(posts):
    cards = []
    for p in posts:
        img = p.get("image", "")
        thumb = (f'<div class="thumb"><img src="{img}" alt="" loading="lazy" '
                 f'width="230" height="144"></div>') if img else ""
        cards.append(
            f'<a class="post-card" href="{SITE}/post/{p["slug"]}">'
            f'{thumb}'
            f'<div class="post-card-body">'
            f'<div class="date">{_fmt_date(p.get("date",""))}</div>'
            f'<h2>{_html.escape(p.get("title", p["slug"]))}</h2>'
            f'<p>{_html.escape(p.get("description",""))}</p></div></a>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Blog — Tennis Analysis Insights | Ten-Fifty5</title>
<meta name="description" content="Guides and insights on tennis match analysis, serve placement, rally patterns, and AI coaching — from the Ten-Fifty5 team.">
<link rel="canonical" href="{SITE}/blog">
<meta name="robots" content="index, follow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Ten-Fifty5">
<meta property="og:title" content="Ten-Fifty5 Blog — Tennis Analysis Insights">
<meta property="og:description" content="Guides and insights on tennis match analysis, serve placement, rally patterns, and AI coaching.">
<meta property="og:url" content="{SITE}/blog">
<meta property="og:image" content="{OG_IMAGE}">
<meta name="twitter:card" content="summary_large_image">
{FAVICON}
{FONT}
<style>{STYLE}</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
{NAV}
<span id="main-content" tabindex="-1"></span>
<header class="idx-head">
  <div class="wrap">
    <div class="eyebrow">Blog</div>
    <h1>Tennis, measured.</h1>
    <p>Guides and insights on match analysis, serve placement, rally patterns, and AI coaching — to help you train against facts, not feelings.</p>
  </div>
</header>
<main>
  <div class="wrap">
    <div class="post-list">
{chr(10).join(cards)}
    </div>
  </div>
</main>
{FOOTER}
{COOKIE_BANNER}
</body>
</html>"""


# ---- helpers ------------------------------------------------------------------

import json as _jsonmod
def _json(s):
    return _jsonmod.dumps(s or "")

_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]
def _fmt_date(d):
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', d or "")
    if not m:
        return ""
    y, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{day} {_MONTHS[mo]} {y}"


def main():
    paths = sorted(glob.glob(os.path.join(POSTS_DIR, "*.md")))
    posts = [parse_post(p) for p in paths]
    posts.sort(key=lambda p: p.get("date", ""), reverse=True)
    for p in posts:
        out = os.path.join(BLOG_DIR, f"{p['slug']}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(render_post(p))
        print(f"  wrote {os.path.relpath(out, BASE_DIR)}")
    with open(os.path.join(BLOG_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(posts))
    print(f"  wrote frontend/blog/index.html ({len(posts)} posts)")


if __name__ == "__main__":
    main()
