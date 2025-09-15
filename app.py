# app.py — WooCommerce-driven planner
# - Products & colors (sizes ignored)
# - Per-color stills: Flat + Model
# - Dynamic Caches → Themes from categories
# - Fixed extras:
#     * per Theme: 5 Product Reels (Model), 3 World (Theme), 1 Main Art (Theme)
#     * per Cache: 3 Cache Art (Cache)
# - Shuffle + generate/export
# - NEW: Lock/unlock rows + Re-roll Unlocked + Clear Unlocked (locks persist)

from flask import Flask, render_template, request, jsonify, send_file
import os, io, csv, json, random
from datetime import datetime
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

app = Flask(__name__)

# ---------------- Env / Woo ----------------
load_dotenv()
WC_URL    = os.getenv("WC_URL", "").rstrip("/")
WC_KEY    = os.getenv("WC_KEY", "")
WC_SECRET = os.getenv("WC_SECRET", "")

def wc_ready():
    return bool(WC_URL and WC_KEY and WC_SECRET and WC_URL.startswith("http"))

def wc_auth():
    return (WC_KEY, WC_SECRET)

def wc_get(route, params=None):
    """GET with pagination."""
    url = urljoin(WC_URL + "/", route.lstrip("/"))
    page = 1
    per_page = 100
    out = []
    while True:
        q = {"per_page": per_page, "page": page}
        if params:
            q.update(params)
        resp = requests.get(url, auth=wc_auth(), params=q, timeout=40)
        if resp.status_code != 200:
            raise RuntimeError(f"WC GET {route} failed {resp.status_code}: {resp.text}")
        data = resp.json()
        if not data:
            break
        out.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return out

# ---------------- Persistence -------------
DATA_FILE = "data.json"

POSTS = []
LOCKS = set()
WC_CATALOG = {
    "products": [],     # normalized list (see _normalize_product)
    "categories": [],   # all wc product categories
    "caches": [],       # [{"id","name"}]
    "themes": [],       # [{"id","name","cache_id","cache_name"}]
    "synced_at": None
}

POST_TYPES = [
    "Product Still (Flat)",
    "Product Still (Model)",
    "Product Reel (Model)",
    "World (Theme)",
    "Main Art (Theme)",
    "Cache Art (Cache)",
]

# Fixed counts
THEME_REELS_PER_THEME = 5
THEME_WORLDS_PER_THEME = 3
THEME_MAINART_PER_THEME = 1
CACHE_ARTS_PER_CACHE = 3

def save_state():
    with open(DATA_FILE, "w") as f:
        json.dump({
            "posts": POSTS,
            "locks": list(LOCKS),
            "wc_catalog": WC_CATALOG
        }, f, indent=2)

def load_state():
    global POSTS, LOCKS, WC_CATALOG
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        POSTS = data.get("posts", [])
        LOCKS = set(data.get("locks", []))
        wc = data.get("wc_catalog", {})
        WC_CATALOG["products"]   = wc.get("products", [])
        WC_CATALOG["categories"] = wc.get("categories", [])
        WC_CATALOG["caches"]     = wc.get("caches", [])
        WC_CATALOG["themes"]     = wc.get("themes", [])
        WC_CATALOG["synced_at"]  = wc.get("synced_at")
    else:
        POSTS[:] = []
        LOCKS.clear()

# ---------------- Woo helpers -------------
def _find_color_attr_name(attrs_dict):
    """Return a key from dict that looks like color/colour."""
    for k in attrs_dict.keys():
        if isinstance(k, str) and ("color" in k.lower() or "colour" in k.lower()):
            return k
    return None

def _extract_variation_color(variation):
    """variation['attributes'] is a list of {name, option}; return color option if present."""
    attrs = variation.get("attributes", [])
    cand = {}
    for a in attrs:
        name = (a.get("name") or "").lower()
        cand[name] = a.get("option") or ""
    key = _find_color_attr_name(cand) or ""
    return (cand.get(key, "") or "").strip()

def _normalize_product(p):
    """
    Normalize a WC product into:
      {
        id, name, cat_ids, cat_names,
        type, colors: [distinct strings], theme_id, theme_name, cache_id, cache_name
      }
    Sizes are ignored. Colors come from:
      - variable product: per-variation color
      - simple product: attribute "Color/Colour" options
    """
    pid = p.get("id")
    name = (p.get("name") or "").strip() or f"Product {pid}"
    ptype = p.get("type")
    cat_ids = [c.get("id") for c in p.get("categories", []) if c.get("id") is not None]
    cat_names = [c.get("name") for c in p.get("categories", []) if c.get("name")]

    colors = []

    if ptype == "variable":
        variations = wc_get(f"/wp-json/wc/v3/products/{pid}/variations")
        seen = set()
        for v in variations:
            c = _extract_variation_color(v)
            if c and c.lower() not in seen:
                seen.add(c.lower())
                colors.append(c)
    else:
        attrs = p.get("attributes", []) or []
        opt_map = {}
        for a in attrs:
            nm = (a.get("name") or "").lower()
            opts = a.get("options") or []
            opt_map[nm] = [str(o).strip() for o in opts if str(o).strip()]
        key = _find_color_attr_name(opt_map) or ""
        options = opt_map.get(key, [])
        seen = set()
        for c in options:
            if c and c.lower() not in seen:
                seen.add(c.lower())
                colors.append(c)

    if not colors:
        colors = [""]

    return {
        "id": pid,
        "name": name,
        "type": ptype,
        "cat_ids": cat_ids,
        "cat_names": cat_names,
        "colors": colors,
        "theme_id": None,
        "theme_name": None,
        "cache_id": None,
        "cache_name": None,
    }

def _build_category_tree(cats):
    by_id = {c["id"]: {**c, "children": []} for c in cats}
    for c in by_id.values():
        pid = c.get("parent")
        if pid and pid in by_id:
            by_id[pid]["children"].append(c)
    return by_id

def _discover_caches_and_themes():
    cats = WC_CATALOG["categories"]
    by_id = _build_category_tree(cats)
    caches_parent = None
    for c in cats:
        if isinstance(c.get("name"), str) and c["name"].lower() == "caches":
            caches_parent = c
            break
    caches = []
    themes = []
    if caches_parent:
        for cache in by_id[caches_parent["id"]]["children"]:
            caches.append({"id": cache["id"], "name": cache["name"]})
            for theme in by_id[cache["id"]]["children"]:
                themes.append({
                    "id": theme["id"],
                    "name": theme["name"],
                    "cache_id": cache["id"],
                    "cache_name": cache["name"],
                })
    WC_CATALOG["caches"] = caches
    WC_CATALOG["themes"] = themes

def _assign_theme_cache_to_products():
    theme_by_id = {t["id"]: t for t in WC_CATALOG["themes"]}
    cache_by_id = {c["id"]: c for c in WC_CATALOG["caches"]}

    for p in WC_CATALOG["products"]:
        theme = None
        for cid in p["cat_ids"]:
            if cid in theme_by_id:
                theme = theme_by_id[cid]
                break
        if theme:
            p["theme_id"] = theme["id"]
            p["theme_name"] = theme["name"]
            p["cache_id"] = theme["cache_id"]
            p["cache_name"] = theme["cache_name"]
        else:
            cache = None
            for cid in p["cat_ids"]:
                if cid in cache_by_id:
                    cache = cache_by_id[cid]
                    break
            if cache:
                p["cache_id"] = cache["id"]
                p["cache_name"] = cache["name"]

def sync_wc():
    cats = wc_get("/wp-json/wc/v3/products/categories", params={"hide_empty": False})
    WC_CATALOG["categories"] = [
        {"id": c.get("id"), "name": c.get("name"), "parent": c.get("parent")}
        for c in cats
    ]

    raw_products = wc_get("/wp-json/wc/v3/products", params={"status": "publish"})
    normalized = [_normalize_product(p) for p in raw_products]
    WC_CATALOG["products"] = normalized

    _discover_caches_and_themes()
    _assign_theme_cache_to_products()

    WC_CATALOG["synced_at"] = datetime.utcnow().isoformat()
    save_state()

# ---------------- Post building ----------
def _uid():
    return hex(random.randint(0, 2**64 - 1))[2:]

def _product_stills():
    posts = []
    for p in WC_CATALOG["products"]:
        garment = p["name"]
        cache = p.get("cache_name") or "Unassigned"
        for color in p["colors"]:
            posts.append({
                "id": f"flat_{p['id']}_{color}_{_uid()}",
                "type": "Product Still (Flat)",
                "garment": garment,
                "color": color,
                "note": "",
                "cache": cache,
                "theme": p.get("theme_name") or "",
            })
            posts.append({
                "id": f"model_{p['id']}_{color}_{_uid()}",
                "type": "Product Still (Model)",
                "garment": garment,
                "color": color,
                "note": "",
                "cache": cache,
                "theme": p.get("theme_name") or "",
            })
    return posts

def _theme_extras():
    posts = []
    prods_by_theme = {}
    for p in WC_CATALOG["products"]:
        t = p.get("theme_name")
        if not t:
            continue
        prods_by_theme.setdefault(t, []).append(p)

    for t in WC_CATALOG["themes"]:
        tname = t["name"]

        pairs = []
        for p in prods_by_theme.get(tname, []):
            for c in p["colors"]:
                pairs.append((p["name"], c, p.get("cache_name") or "Unassigned"))

        if pairs:
            random.shuffle(pairs)

        # 5 Product Reel (Model)
        for i in range(THEME_REELS_PER_THEME):
            if pairs:
                g, c, cache = pairs[i % len(pairs)]
            else:
                g, c, cache = "", "", "Unassigned"
            posts.append({
                "id": f"treel_{t['id']}_{i}_{_uid()}",
                "type": "Product Reel (Model)",
                "garment": g,
                "color": c,
                "note": "",
                "cache": cache,
                "theme": tname,
            })

        # 3 World (Theme)
        for i in range(THEME_WORLDS_PER_THEME):
            posts.append({
                "id": f"tworld_{t['id']}_{i}_{_uid()}",
                "type": "World (Theme)",
                "garment": "",
                "color": "",
                "note": "",
                "cache": t.get("cache_name") or "Unassigned",
                "theme": tname,
            })

        # 1 Main Art (Theme)
        for i in range(THEME_MAINART_PER_THEME):
            posts.append({
                "id": f"tmain_{t['id']}_{i}_{_uid()}",
                "type": "Main Art (Theme)",
                "garment": "",
                "color": "",
                "note": "",
                "cache": t.get("cache_name") or "Unassigned",
                "theme": tname,
            })

    return posts

def _cache_extras():
    posts = []
    for c in WC_CATALOG["caches"]:
        cname = c["name"]
        for i in range(CACHE_ARTS_PER_CACHE):
            posts.append({
                "id": f"cworld_{c['id']}_{i}_{_uid()}",
                "type": "Cache Art (Cache)",
                "garment": "",
                "color": "",
                "note": "",
                "cache": cname,
                "theme": "",
            })
    return posts

def build_posts():
    all_posts = []
    all_posts += _product_stills()
    all_posts += _theme_extras()
    all_posts += _cache_extras()
    random.shuffle(all_posts)
    return all_posts

# ---------------- Routes -----------------
@app.route("/", methods=["GET", "POST"])
def index():
    global POSTS, LOCKS
    msg = ""

    if request.method == "POST":
        act = request.form.get("action")

        if act == "sync_wc":
            try:
                if not wc_ready():
                    msg = "WooCommerce not configured (.env WC_URL, WC_KEY, WC_SECRET)."
                else:
                    sync_wc()
                    msg = f"Synced: {len(WC_CATALOG['products'])} products, {len(WC_CATALOG['caches'])} caches, {len(WC_CATALOG['themes'])} themes."
            except Exception as e:
                msg = f"Sync failed: {e}"

        elif act == "generate":
            try:
                if not WC_CATALOG["products"]:
                    if not wc_ready():
                        msg = "WooCommerce not configured; cannot generate."
                    else:
                        sync_wc()
                POSTS = build_posts()
                LOCKS = set()
                if not msg:
                    msg = f"Generated {len(POSTS)} posts."
            except Exception as e:
                msg = f"Generate failed: {e}"

        elif act == "lock_toggle":
            pid = request.form.get("post_id")
            if pid:
                if pid in LOCKS: LOCKS.remove(pid)
                else: LOCKS.add(pid)

        elif act == "reroll":
            # shuffle only unlocked rows, preserve locked positions
            unlocked_idx = [i for i, p in enumerate(POSTS) if p["id"] not in LOCKS]
            unlocked = [POSTS[i] for i in unlocked_idx]
            random.shuffle(unlocked)
            new_posts = POSTS[:]
            for i, idx in enumerate(unlocked_idx):
                new_posts[idx] = unlocked[i]
            POSTS = new_posts
            msg = "Re-rolled unlocked posts."

        elif act == "clear_unlocked":
            POSTS[:] = [p for p in POSTS if p["id"] in LOCKS]
            msg = "Cleared all unlocked posts."

        save_state()

    return render_template(
        "index.html",
        posts=POSTS,
        locks=LOCKS,
        msg=msg,
        wc_ready=wc_ready(),
        wc_synced=bool(WC_CATALOG["products"]),
        wc_count=len(WC_CATALOG["products"]),
        wc_synced_at=WC_CATALOG.get("synced_at"),
        post_count=len(POSTS),
    )

@app.route("/export")
def export_csv():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["#", "Type", "Name (Garment/Theme/Cache)", "Color", "Locked"])
    for idx, p in enumerate(POSTS):
        name = p.get("garment") or p.get("theme") or p.get("cache") or ""
        cw.writerow([
            idx+1,
            p.get("type",""),
            name,
            p.get("color",""),
            "Yes" if p.get("id") in LOCKS else "",
        ])
    output = io.BytesIO(si.getvalue().encode("utf-8"))
    output.seek(0)
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="blahpunk_posts.csv")

if __name__ == "__main__":
    load_state()
    app.run(debug=True)
