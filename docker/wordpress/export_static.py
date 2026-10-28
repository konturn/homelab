#!/usr/bin/env python3
"""Export blog.nkontur.com from a running WordPress container to static files.

The public site is the output of this script, served by the external nginx
from docker/nginx/sites/blog.nkontur.com/. WordPress itself is not running in
normal operation; see docker/wordpress/README.md for bringing it up on the LAN
to edit and re-export.

The crawl talks to the container directly over its compose bridge network
(http://<container ip>/ with `Host: blog.nkontur.com`). wp-config.php forces
HTTPS and pins WP_HOME, so the pages it returns already carry the real
https://blog.nkontur.com URLs, which are kept as they are.

What it writes, under --out:
  - every page, post, archive, feed and sitemap reachable from the home page,
    the sitemaps and the feeds, as <path>/index.html (feeds: <path>/index.xml)
  - every same-site asset those reference (CSS, JS, fonts, images incl. srcset
    sizes), saved without their ?ver= query strings, which are removed from
    the references too
It skips dynamic endpoints (wp-json, xmlrpc, wp-login, wp-admin, search,
oEmbed, ?p= links) and removes the <link> tags that
advertise them. Draft and private posts are never fetched: the crawl only
sees what an anonymous visitor sees.

Usage (on the router, WordPress running):
  python3 export_static.py --out /tmp/blog-static
Then replace docker/nginx/sites/blog.nkontur.com/ with the output and open an MR.
"""
import argparse
import collections
import html
import html.parser
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://blog.nkontur.com"
HOST = "blog.nkontur.com"

SEEDS = [
    "/", "/robots.txt", "/wp-sitemap.xml", "/index.php/feed/", "/index.php/comments/feed/",
    # /favicon.ico is a WordPress redirect to this image; nginx keeps the redirect.
    "/wp-includes/images/w-logo-gray-white-bg.png",
]

# Dynamic or private endpoints: never fetched, never saved.
REJECT = re.compile(
    r"/wp-json(/|$)|xmlrpc\.php|wp-login\.php|/wp-admin(/|$)|wp-cron\.php|wp-comments-post\.php"
    r"|/embed/?$|/trackback/?$"
)
# The only query string a static file can stand in for is an asset version.
ALLOWED_QUERY_KEYS = {"ver"}

ABS_URL = re.compile(r"(?:https?:)?//blog\.nkontur\.com(?:/[^\s\"'<>()\\]*)?")
ESC_URL = re.compile(r"(?:https?:)?\\/\\/blog\.nkontur\.com(?:\\/[^\s\"'<>()\\]*)*")
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)|@import\s+['\"]([^'\"]+)['\"]")

# <link> tags pointing at endpoints the static site does not have.
DEAD_LINK_TAGS = re.compile(
    r"<link[^>]+(?:rel=[\"'](?:https://api\.w\.org/|EditURI|pingback)[\"']"
    r"|application/json\+oembed|text/xml\+oembed|/wp-json/)[^>]*>\n?",
    re.I,
)
# ?ver=... on a same-site or root-relative URL (also HTML- and JSON-escaped).
VER_QUERY = re.compile(r"(?:\?|&#0?38;|&amp;|&)ver=[\w.\-]+")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class LinkParser(html.parser.HTMLParser):
    ATTRS = {"href", "src", "data-src", "poster", "content"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if not v:
                continue
            if k == "content" and not v.startswith(("http", "/")):
                continue  # meta content that is not a URL (viewport, description)
            if k in self.ATTRS:
                self.links.append(v)
            elif k in ("srcset", "data-srcset"):
                self.links += [p.strip().split(" ")[0] for p in v.split(",") if p.strip()]


def container_origin(name):
    out = subprocess.run(
        ["docker", "inspect", "-f",
         '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}\n{{end}}', name],
        check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        net, _, ip = line.partition(" ")
        if net.endswith("wordpress_internal") and ip:
            return f"http://{ip}"
    sys.exit(f"{name} is not on the wordpress_internal network (is it running?)")


def normalize(ref, base):
    """Same-site absolute URL path (+ allowed query), or None."""
    ref = html.unescape(ref.strip()).replace("\\/", "/")
    for junk in ('"', "'", "&quot;"):
        ref = ref.split(junk)[0]
    if not ref or ref.startswith(("#", "mailto:", "tel:", "data:", "javascript:")):
        return None
    u = urllib.parse.urlsplit(urllib.parse.urljoin(base, ref))
    if u.scheme not in ("http", "https") or u.hostname != HOST:
        return None
    path = urllib.parse.quote(urllib.parse.unquote(u.path or "/"), safe="/:@!$&'()*+,;=~")
    if REJECT.search(path):
        return None
    q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if set(q) - ALLOWED_QUERY_KEYS:
        return None
    return path + ("?" + u.query if u.query else "")


def local_path(url_path, ctype):
    path = urllib.parse.unquote(url_path.split("?", 1)[0])
    if path.endswith("/"):
        xml = "xml" in ctype and "html" not in ctype
        path += "index.xml" if xml else "index.html"
    rel = os.path.normpath(path.lstrip("/"))
    if rel.startswith(".."):
        raise ValueError(path)
    return rel


def extract(body, ctype, url):
    text = body.decode("utf-8", errors="replace")
    refs = ABS_URL.findall(text) + ESC_URL.findall(text)
    if "html" in ctype:
        p = LinkParser()
        p.feed(text)
        refs += p.links
    if "css" in ctype or "html" in ctype:
        refs += [a or b for a, b in CSS_URL.findall(text)]
    return refs


def rewrite(text):
    text = text.replace("http://blog.nkontur.com", SITE).replace("http:\\/\\/blog.nkontur.com", "https:\\/\\/blog.nkontur.com")
    text = DEAD_LINK_TAGS.sub("", text)

    # Drop ?ver= only on same-site / root-relative URLs: walk each URL-ish token.
    def strip_ver(m):
        tok = m.group(0)
        if "blog.nkontur.com" in tok or tok.startswith(("/", "\\/")):
            tok = VER_QUERY.sub("", tok, count=1)
        return tok
    return re.sub(r"(?:https?:)?(?:\\?/)[^\s\"'<>()]*?(?:\?|&#0?38;|&amp;)ver=[\w.\-]+", strip_ver, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="output directory (must not exist)")
    ap.add_argument("--origin", help="WordPress origin, default: the wordpress container's bridge IP")
    ap.add_argument("--container", default="wordpress")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between requests")
    args = ap.parse_args()
    if os.path.exists(args.out):
        sys.exit(f"{args.out} exists; pass a new directory")
    origin = args.origin or container_origin(args.container)
    opener = urllib.request.build_opener(NoRedirect)

    queue = collections.deque(SEEDS)
    seen = set(SEEDS)
    derived = set()  # parent archives guessed from page paths; a 404 there is fine
    saved, redirects, errors = {}, {}, []
    while queue:
        path = queue.popleft()
        req = urllib.request.Request(origin + path, headers={
            "Host": HOST, "X-Forwarded-Proto": "https", "User-Agent": "blog-static-export"})
        try:
            resp = opener.open(req, timeout=30)
            status, body, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, body, headers = e.code, e.read(), e.headers
        time.sleep(args.delay)
        ctype = (headers.get("Content-Type") or "").lower()
        if status in (301, 302, 307, 308):
            target = normalize(headers.get("Location", ""), SITE + path)
            redirects[path] = headers.get("Location", "")
            refs = [headers.get("Location", "")]
        elif status == 200:
            rel = local_path(path, ctype)
            if rel in saved.values() and path.split("?")[0] != path:
                continue  # same file, other ?ver=
            saved[path] = rel
            dest = os.path.join(args.out, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            refs = extract(body, ctype, SITE + path)
            if any(t in ctype for t in ("html", "css", "javascript", "xml", "json")) or rel.endswith(".txt"):
                body = rewrite(body.decode("utf-8")).encode("utf-8")
            with open(dest, "wb") as f:
                f.write(body)
        else:
            if path not in derived:
                errors.append((status, path))
            continue
        # Date and parent-category archives are not always linked
        # (/index.php/2021/01/04/<slug>/ -> /index.php/2021/01/04/, /2021/01/, /2021/).
        if "html" in ctype and path.startswith("/index.php/") and path.endswith("/"):
            parent = path.rstrip("/").rsplit("/", 1)[0] + "/"
            if parent not in seen and parent.count("/") > 2:
                seen.add(parent)
                derived.add(parent)
                queue.append(parent)
        for ref in refs:
            n = normalize(ref, SITE + path)
            if n and n not in seen:
                seen.add(n)
                queue.append(n)

    # The theme's "not found" page, served by nginx as error_page 404.
    req = urllib.request.Request(origin + "/index.php/static-export-missing-page/", headers={
        "Host": HOST, "X-Forwarded-Proto": "https", "User-Agent": "blog-static-export"})
    try:
        opener.open(req, timeout=30)
        errors.append(("200 instead of 404", "/index.php/static-export-missing-page/"))
    except urllib.error.HTTPError as e:
        with open(os.path.join(args.out, "404.html"), "w", encoding="utf-8") as f:
            f.write(rewrite(e.read().decode("utf-8")))

    # Old ?p=<id> shortlinks, for the map in docker/nginx/router.lab.nkontur.com/external_http.conf.
    shortlinks = {}
    for path, rel in saved.items():
        if rel.endswith("index.html"):
            with open(os.path.join(args.out, rel), encoding="utf-8") as f:
                m = re.search(r"rel=.shortlink. href=.https://blog\.nkontur\.com/\?p=(\d+)", f.read())
            if m:
                shortlinks[m.group(1)] = path

    for root, dirs, files in os.walk(args.out):
        os.chmod(root, 0o755)
        for fn in files:
            os.chmod(os.path.join(root, fn), 0o644)
    print(f"saved {len(saved)} files, {len(redirects)} redirects, {len(errors)} errors")
    for p, loc in sorted(redirects.items()):
        print(f"  redirect {p} -> {loc}")
    print("shortlinks (map $arg_p $blog_shortlink):")
    for pid, path in sorted(shortlinks.items(), key=lambda kv: int(kv[0])):
        print(f"    {pid} {path};")
    for st, p in errors:
        print(f"  {st} {p}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
