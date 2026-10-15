"""Build site/index.html from the template + the exported dashboard data."""
import os
root = os.path.dirname(os.path.abspath(__file__))
t = open(os.path.join(root, "site", "template.html"), encoding="utf-8").read()
d = open(os.path.join(root, "site", "dashboard_data.json"), encoding="utf-8").read().replace("</", "<\\/")
page = t.replace("/*__DATA__*/null", d)
html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
        "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>"
        "</head><body>" + page + "</body></html>")
open(os.path.join(root, "site", "index.html"), "w", encoding="utf-8").write(html)
print("site/index.html", len(html) // 1024, "KB")
