"""
Учёт продаж страйкбольного/охотничьего снаряжения.
Один файл. Запуск: python -m uvicorn app:app --reload
Открыть: http://127.0.0.1:8000
"""

import os
import json
import sqlite3
import shutil
import uuid
from datetime import date
from contextlib import contextmanager

import jinja2
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import DictLoader

DB_PATH = "shop.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    sku TEXT,
    sale_price REAL DEFAULT 0,
    image_url TEXT,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    purchase_date TEXT NOT NULL,
    qty_initial INTEGER NOT NULL,
    qty_remaining INTEGER NOT NULL,
    purchase_price REAL NOT NULL,
    supplier TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date TEXT NOT NULL,
    delivered_date TEXT,
    source TEXT,
    customer_name TEXT,
    customer_phone TEXT,
    customer_link TEXT,
    status TEXT NOT NULL DEFAULT 'collecting',
    subtotal REAL NOT NULL DEFAULT 0,
    discount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    comment TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    sale_price REAL NOT NULL,
    cost_total REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_item_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_item_id INTEGER NOT NULL REFERENCES order_items(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    qty INTEGER NOT NULL,
    purchase_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS defects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id INTEGER REFERENCES batches(id),
    qty INTEGER NOT NULL,
    reason TEXT,
    defect_date TEXT NOT NULL,
    cost_total REAL NOT NULL DEFAULT 0,
    order_id INTEGER REFERENCES orders(id),
    comment TEXT
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    batch_id INTEGER,
    type TEXT NOT NULL,
    qty INTEGER NOT NULL,
    movement_date TEXT NOT NULL,
    ref_order_id INTEGER,
    ref_defect_id INTEGER
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


init_db()

# ============================================================
# ЛОГИКА FIFO
# ============================================================

def write_off_fifo(conn, product_id: int, qty: int, order_item_id: int, order_id: int, movement_date: str | None = None):
    if not movement_date:
        movement_date = str(date.today())

    rows = conn.execute(
        "SELECT * FROM batches WHERE product_id=? AND qty_remaining>0 "
        "ORDER BY purchase_date, id",
        (product_id,),
    ).fetchall()

    available = sum(r["qty_remaining"] for r in rows)
    if available < qty:
        raise HTTPException(400, f"Недостаточно товара на складе: нужно {qty}, есть {available}")

    remaining = qty
    cost_total = 0.0
    for b in rows:
        if remaining == 0:
            break
        take = min(b["qty_remaining"], remaining)
        conn.execute("UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?", (take, b["id"]))
        conn.execute(
            "INSERT INTO order_item_batches (order_item_id, batch_id, qty, purchase_price) "
            "VALUES (?,?,?,?)",
            (order_item_id, b["id"], take, b["purchase_price"]),
        )
        conn.execute(
            "INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date, ref_order_id) "
            "VALUES (?,?,?,?,?,?)",
            (product_id, b["id"], "sale", -take, movement_date, order_id),
        )
        cost_total += take * b["purchase_price"]
        remaining -= take

    return cost_total


def return_to_batches(conn, order_id: int):
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    total_cost = 0.0
    for it in items:
        breakdown = conn.execute(
            "SELECT * FROM order_item_batches WHERE order_item_id=?", (it["id"],)
        ).fetchall()
        for bd in breakdown:
            conn.execute(
                "UPDATE batches SET qty_remaining=qty_remaining+? WHERE id=?",
                (bd["qty"], bd["batch_id"]),
            )
            conn.execute(
                "INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date, ref_order_id) "
                "VALUES (?,?,?,?,?,?)",
                (it["product_id"], bd["batch_id"], "return", bd["qty"], str(date.today()), order_id),
            )
            total_cost += bd["qty"] * bd["purchase_price"]
    return total_cost


# ============================================================
# КОНСТАНТЫ
# ============================================================

SOURCE_LABEL = {
    "avito": "Авито",
    "telegram": "Telegram",
    "vk": "ВК",
    "call": "Звонок",
    "other": "Другое",
}
STATUSES = [
    ("collecting", "Собирается"),
    ("shipped", "Отправлен"),
    ("delivered", "Вручён"),
    ("returned", "Возвращён"),
    ("cancelled", "Отменён"),
]
STATUS_LABEL = dict(STATUSES)
STATUS_TAG = {
    "collecting": "tag-yellow",
    "shipped": "tag-blue",
    "delivered": "tag-green",
    "returned": "tag-red",
    "cancelled": "tag-gray",
}
REASON_LABEL = {
    "supplier": "Брак поставщика",
    "transport": "Повреждение при транспортировке",
    "return": "Возврат от клиента",
    "other": "Другое",
}

# ============================================================
# HTML-ШАБЛОНЫ
# ============================================================

BASE_HEAD = """
<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Учёт продаж</title>
<style>
  :root{
    --bg:#0b0f0d;
    --panel:#121814;
    --panel-2:#161d18;
    --line:#233029;
    --text:#e6efe9;
    --muted:#8fa39a;
    --accent:#22c55e;
    --accent-2:#16a34a;
    --accent-soft:rgba(34,197,94,.12);
    --danger:#ef4444;
  }
  *{box-sizing:border-box}
  body{
    margin:0;background:radial-gradient(1200px 600px at 20% -10%, #0f1a14 0%, var(--bg) 55%) fixed;
    color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
    font-size:14px;line-height:1.45;
  }
  a{color:var(--accent);text-decoration:none}
  a:hover{color:#4ade80}

  header{
    position:sticky;top:0;z-index:50;
    background:rgba(11,15,13,.85);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);
    padding:0 24px;height:60px;display:flex;align-items:center;gap:28px;
  }
  header .brand{
    font-weight:700;letter-spacing:.5px;font-size:15px;color:#fff;
    display:flex;align-items:center;gap:10px;
  }
  header .brand::before{
    content:"";width:10px;height:10px;border-radius:2px;
    background:var(--accent);box-shadow:0 0 12px rgba(34,197,94,.8);
  }
  header nav{display:flex;gap:6px;margin-left:auto}
  header nav a{
    color:var(--muted);font-weight:500;padding:8px 14px;border-radius:8px;
    transition:background .15s ease, color .15s ease;
  }
  header nav a:hover{color:#fff;background:var(--accent-soft)}
  header nav a.active{color:#fff;background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--line)}

  .wrap{max-width:1240px;margin:28px auto 60px;padding:0 20px}
  h1{font-size:24px;font-weight:700;margin:0 0 20px;letter-spacing:.3px}
  h2{font-size:16px}

  .card{
    background:linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%);
    border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:20px;
    box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 10px 30px -20px rgba(0,0,0,.9);
  }

  table{width:100%;border-collapse:separate;border-spacing:0}
  th,td{padding:11px 12px;text-align:left;font-size:13.5px;vertical-align:top;
        border-bottom:1px solid var(--line)}
  th{
    background:#0f1512;color:var(--muted);font-weight:600;font-size:12px;
    text-transform:uppercase;letter-spacing:.6px;
  }
  tr:hover td{background:rgba(34,197,94,.04)}
  tbody tr:last-child td{border-bottom:0}

  input,select,textarea{
    padding:9px 11px;background:#0d1310;color:var(--text);
    border:1px solid var(--line);border-radius:8px;font-size:14px;width:100%;
    transition:border-color .15s ease, box-shadow .15s ease;font-family:inherit;
  }
  input:focus,select:focus,textarea:focus{
    outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(34,197,94,.18)
  }
  input::placeholder{color:#4b5f55}
  select option{background:#0d1310;color:var(--text)}

  label{display:block;font-size:12px;color:var(--muted);margin:10px 0 6px;
        text-transform:uppercase;letter-spacing:.6px;font-weight:600}

  button,.btn{
    background:linear-gradient(180deg,var(--accent) 0%, var(--accent-2) 100%);
    color:#04140a;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;
    font-size:13.5px;font-weight:700;letter-spacing:.2px;text-decoration:none;
    display:inline-block;transition:transform .05s ease, filter .15s ease;
    box-shadow:0 1px 0 rgba(255,255,255,.15) inset, 0 6px 16px -10px rgba(34,197,94,.8);
  }
  button:hover,.btn:hover{filter:brightness(1.08)}
  button:active,.btn:active{transform:translateY(1px)}
  .btn-gray{background:#1e2a23;color:var(--text);box-shadow:inset 0 0 0 1px var(--line)}
  .btn-gray:hover{background:#243229}
  .btn-danger{background:linear-gradient(180deg,#ef4444 0%, #b91c1c 100%);color:#fff}
  .btn-sm{padding:6px 10px;font-size:12px;border-radius:6px}

  .row{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
  .row-3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .muted{color:var(--muted);font-size:12.5px}
  .flex{display:flex;gap:8px;align-items:center}
  .right{text-align:right}
  .thumb{width:44px;height:44px;object-fit:cover;border-radius:8px;
         background:#0d1310;border:1px solid var(--line)}

  .tag{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11.5px;
       font-weight:700;letter-spacing:.3px;text-transform:uppercase}
  .tag-green{background:rgba(34,197,94,.15);color:#4ade80;box-shadow:inset 0 0 0 1px rgba(34,197,94,.35)}
  .tag-red{background:rgba(239,68,68,.15);color:#f87171;box-shadow:inset 0 0 0 1px rgba(239,68,68,.35)}
  .tag-yellow{background:rgba(245,158,11,.15);color:#fbbf24;box-shadow:inset 0 0 0 1px rgba(245,158,11,.35)}
  .tag-blue{background:rgba(59,130,246,.15);color:#60a5fa;box-shadow:inset 0 0 0 1px rgba(59,130,246,.35)}
  .tag-gray{background:rgba(148,163,184,.12);color:#cbd5e1;box-shadow:inset 0 0 0 1px rgba(148,163,184,.3)}

  td.right, th.right{font-variant-numeric:tabular-nums}
  td b{color:#fff}

  /* ---- карточки товаров ---- */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:18px}
  .prod{
    background:linear-gradient(180deg,var(--panel) 0%,var(--panel-2) 100%);
    border:1px solid var(--line);border-radius:14px;overflow:hidden;
    display:flex;flex-direction:column;
    transition:border-color .15s, transform .1s, box-shadow .15s;
    box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 12px 30px -22px rgba(0,0,0,.9);
  }
  .prod:hover{border-color:#2e4237;transform:translateY(-2px);
              box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 18px 40px -22px rgba(0,0,0,.9)}
  .prod-head{padding:14px 16px 10px;border-bottom:1px solid var(--line)}
  .prod-title{font-size:15px;font-weight:700;color:#fff;line-height:1.3}
  .prod-sku{font-size:11.5px;color:var(--muted);margin-top:2px;letter-spacing:.3px}
  .prod-img{
    height:240px;background:#0a0f0c center/cover no-repeat;position:relative;
    border-bottom:1px solid var(--line);
  }
  .prod-img .noimg{
    position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:#243229;font-size:46px;font-weight:800;letter-spacing:3px;
  }
  .prod-img .stock{
    position:absolute;top:10px;right:10px;font-size:11px;font-weight:700;
    padding:4px 10px;border-radius:999px;background:rgba(34,197,94,.92);color:#04140a;
  }
  .prod-img .stock.low{background:rgba(245,158,11,.95);color:#1a1200}
  .prod-img .stock.out{background:rgba(239,68,68,.92);color:#fff}
  .prod-body{padding:14px 16px;display:flex;flex-direction:column;gap:12px;flex:1}
  .prod-prices{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .prod-prices .cell{background:#0d1310;border:1px solid var(--line);border-radius:8px;
                     padding:8px 10px}
  .prod-prices .lbl{font-size:10.5px;color:var(--muted);text-transform:uppercase;
                    letter-spacing:.6px;font-weight:600}
  .prod-prices .val{font-size:14px;font-weight:700;color:#fff;
                    font-variant-numeric:tabular-nums;margin-top:2px}
  .prod-prices .val.green{color:#4ade80}
  .prod-section-title{font-size:11px;color:var(--muted);text-transform:uppercase;
                      letter-spacing:.7px;font-weight:700;margin-top:2px}
  .batch-row{font-size:12px;color:#b8c6be;display:flex;justify-content:space-between;
             align-items:center;padding:5px 8px;border-radius:6px;background:#0d1310;
             border:1px solid var(--line);margin-bottom:4px;font-variant-numeric:tabular-nums}
  .batch-row b{color:#fff}
  .batch-row a{color:var(--accent);margin-left:6px}
  .batch-form{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  .batch-form input{padding:7px 9px;font-size:12.5px}
  .batch-form .full{grid-column:1 / -1}
  .batch-form button{padding:8px;font-size:12.5px}
  .batch-hint{grid-column:1 / -1;font-size:11px;color:var(--muted);line-height:1.35}
  .prod-actions{display:flex;gap:8px;padding:12px 16px 16px;border-top:1px solid var(--line);
                margin-top:auto}
  .prod-actions form{flex:0 0 auto}
  .prod-actions .btn{flex:1;text-align:center}
  .empty{text-align:center;padding:60px 20px;color:var(--muted)}
  .empty b{display:block;color:var(--text);font-size:16px;margin-bottom:6px}

  /* ---- группировка по категориям ---- */
  .cat-nav{
    display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;
    padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  }
  .cat-nav a{
    font-size:12.5px;font-weight:600;color:#cbd5e1;
    padding:5px 11px;border-radius:999px;background:#0d1310;
    border:1px solid var(--line);transition:all .15s ease;
    display:inline-flex;align-items:center;gap:7px;
  }
  .cat-nav a:hover{color:#fff;border-color:var(--accent);background:var(--accent-soft)}
  .cat-nav .cat-count{
    font-size:10.5px;font-weight:700;color:#4ade80;
    background:rgba(34,197,94,.15);padding:1px 7px;border-radius:999px;
  }
  .cat-block{margin-bottom:32px;scroll-margin-top:80px}
  .cat-head{
    display:flex;justify-content:space-between;align-items:flex-end;
    padding:0 4px 12px;margin-bottom:14px;border-bottom:1px solid var(--line);
  }
  .cat-title{font-size:18px;font-weight:800;color:#fff;letter-spacing:.3px;
             text-transform:capitalize}
  .cat-sub{font-size:12px;color:var(--muted);margin-top:3px}
  .cat-value{font-size:16px;font-weight:800;color:#4ade80;
             font-variant-numeric:tabular-nums}

  /* ---- общая стоимость склада ---- */
  .warehouse-total{
    display:flex;justify-content:space-between;align-items:center;gap:24px;
    padding:20px 24px;margin-bottom:24px;flex-wrap:wrap;
    background:linear-gradient(180deg,var(--panel) 0%,var(--panel-2) 100%);
    border:1px solid var(--line);border-radius:14px;
    box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 10px 30px -22px rgba(0,0,0,.9);
  }
  .wt-main .wt-lbl{
    font-size:11.5px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.7px;font-weight:700;
  }
  .wt-main .wt-val{
    font-size:30px;font-weight:800;color:#fff;line-height:1.1;margin-top:4px;
    font-variant-numeric:tabular-nums;letter-spacing:-.5px;
  }
  .wt-stats{display:flex;gap:28px;flex-wrap:wrap}
  .wt-stat{text-align:right}
  .wt-stat .wt-lbl{
    font-size:11px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.6px;font-weight:600;
  }
  .wt-stat .wt-num{
    font-size:20px;font-weight:800;color:#fff;margin-top:3px;
    font-variant-numeric:tabular-nums;
  }

  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:var(--bg)}
  ::-webkit-scrollbar-thumb{background:#1e2a23;border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:#2a3a30}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head><body>
<header>
  <div class="brand">ARMORY · учёт</div>
  <nav>
    <a href="/" class="{{ 'active' if request.url.path == '/' else '' }}">Склад</a>
    <a href="/orders" class="{{ 'active' if request.url.path.startswith('/orders') else '' }}">Заказы</a>
    <a href="/defects" class="{{ 'active' if request.url.path.startswith('/defects') else '' }}">Брак</a>
    <a href="/reports" class="{{ 'active' if request.url.path.startswith('/reports') else '' }}">Отчёты</a>
  </nav>
</header>
<div class="wrap">
"""

BASE_FOOT = "</div></body></html>"

BATCH_CALC_JS = """
<script>
(function(){
  window.toggleBatch = function(pid){
    const f = document.getElementById('bform'+pid);
    const b = document.getElementById('tbtn'+pid);
    if(!f) return;
    const open = f.style.display !== 'none';
    f.style.display = open ? 'none' : 'block';
    if(b) b.textContent = open ? '+' : '×';
    if(!open){
      const first = f.querySelector('input[name="purchase_date"]');
      if(first) first.focus();
    }
  };

  function bind(qtyId, priceId, totalId){
    const q = document.getElementById(qtyId);
    const p = document.getElementById(priceId);
    const t = document.getElementById(totalId);
    if(!q || !p || !t) return;
    let lastEdited = null;
    function recalcPriceToTotal(){
      const qv = parseFloat(q.value) || 0;
      const pv = parseFloat(p.value) || 0;
      if(qv > 0) t.value = (qv * pv).toFixed(2);
    }
    function recalcTotalToPrice(){
      const qv = parseFloat(q.value) || 0;
      const tv = parseFloat(t.value) || 0;
      if(qv > 0) p.value = (tv / qv).toFixed(2);
    }
    q.addEventListener('input', function(){
      if(lastEdited === 'total') recalcTotalToPrice();
      else recalcPriceToTotal();
    });
    p.addEventListener('input', function(){ lastEdited='price'; recalcPriceToTotal(); });
    t.addEventListener('input', function(){ lastEdited='total'; recalcTotalToPrice(); });
  }

  document.querySelectorAll('[data-batch-calc]').forEach(function(el){
    bind(el.dataset.qty, el.dataset.price, el.dataset.total);
  });
})();
</script>
"""

INDEX_TPL = BASE_HEAD + """
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
  <h1 style="margin:0;">Склад</h1>
  <a href="/audit" class="btn btn-gray" style="border:1px solid var(--line);">🔍 Сверка склада</a>
</div>

{% if totals and totals.products > 0 %}
<div class="warehouse-total">
  <div class="wt-main">
    <div class="wt-lbl">Общая стоимость склада</div>
    <div class="wt-val">{{ '%.2f'|format(totals.stock_value) }} ₽</div>
  </div>
  <div class="wt-stats">
    <div class="wt-stat">
      <div class="wt-lbl">Товаров</div>
      <div class="wt-num">{{ totals.products }}</div>
    </div>
    <div class="wt-stat">
      <div class="wt-lbl">Категорий</div>
      <div class="wt-num">{{ totals.categories }}</div>
    </div>
    <div class="wt-stat">
      <div class="wt-lbl">Всего штук</div>
      <div class="wt-num">{{ totals.qty }}</div>
    </div>
  </div>
</div>
{% endif %}

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Добавить товар</h2>
  <form method="post" action="/products" enctype="multipart/form-data">
    <div class="row-3">
      <div><label>Название</label><input name="name" required></div>
      <div><label>Категория</label>
        <select name="category">
          <option>прицелы</option><option>фонари</option><option>планки</option>
          <option>рукоятки</option><option>глушители</option><option>другое</option>
        </select></div>
      <div><label>Артикул / SKU</label><input name="sku"></div>
    </div>
    <div class="row-3">
      <div><label>Цена продажи (базовая)</label><input name="sale_price" type="number" step="0.01" value="0"></div>
      <div><label>Картинка</label><input name="image" type="file" accept="image/*"></div>
      <div><label>Описание</label><input name="description"></div>
    </div>
    <div style="margin-top:12px"><button>Добавить товар</button></div>
  </form>
</div>

{% if not groups %}
  <div class="card empty">
    <b>Товаров пока нет</b>
    Добавь первый товар через форму выше — и он появится здесь карточкой.
  </div>
{% else %}

<div class="cat-nav">
  {% for g in groups %}
    <a href="#cat-{{ loop.index }}">{{ g.name }} <span class="cat-count">{{ g.count }}</span></a>
  {% endfor %}
</div>

{% for g in groups %}
<div class="cat-block" id="cat-{{ loop.index }}">
  <div class="cat-head">
    <div>
      <div class="cat-title">{{ g.name }}</div>
      <div class="cat-sub">{{ g.count }} товар(ов) · {{ g.total_qty }} шт на складе</div>
    </div>
    <div class="cat-value">{{ '%.2f'|format(g.total_stock_value) }} ₽</div>
  </div>

  <div class="grid">
    {% for p in g.products %}
    <div class="prod">
      <div class="prod-head">
        <div class="prod-title">{{p.name}}</div>
        {% if p.sku %}<div class="prod-sku">SKU: {{p.sku}}</div>{% endif %}
      </div>

      <div class="prod-img"{% if p.image_url %} style="background-image:url('{{p.image_url}}')"{% endif %}>
        {% if not p.image_url %}<div class="noimg">НЕТ ФОТО</div>{% endif %}
        {% if p.qty == 0 %}
          <span class="stock out">нет в наличии</span>
        {% elif p.qty <= 3 %}
          <span class="stock low">{{p.qty}} шт</span>
        {% else %}
          <span class="stock">{{p.qty}} шт</span>
        {% endif %}
      </div>

      <div class="prod-body">
        <div class="prod-prices">
          <div class="cell">
            <div class="lbl">Цена продажи</div>
            <div class="val green">{{ '%.2f'|format(p.sale_price) }} ₽</div>
          </div>
          <div class="cell">
            <div class="lbl">Ср. закупка</div>
            <div class="val">{% if p.avg_purchase %}{{ '%.2f'|format(p.avg_purchase) }} ₽{% else %}—{% endif %}</div>
          </div>
          <div class="cell">
            <div class="lbl">Посл. закупка</div>
            <div class="val">{% if p.last_purchase %}{{ '%.2f'|format(p.last_purchase) }} ₽{% else %}—{% endif %}</div>
          </div>
          <div class="cell">
            <div class="lbl">Стоимость остатка</div>
            <div class="val">{{ '%.2f'|format(p.stock_value) }} ₽</div>
          </div>
        </div>

        <div>
          <div class="prod-section-title" style="display:flex;justify-content:space-between;align-items:center">
            <span>Партии (FIFO)</span>
            <button type="button" class="btn btn-sm" style="padding:2px 9px;font-size:13px;line-height:1"
                    onclick="toggleBatch({{p.id}})" id="tbtn{{p.id}}" title="Добавить партию">+</button>
          </div>

          {% for b in p.batches %}
            <div class="batch-row">
              <span>{{b.purchase_date}} · {{b.qty_remaining}}/{{b.qty_initial}} × {{ '%.2f'|format(b.purchase_price) }} ₽</span>
              <span><b>{{ '%.2f'|format(b.qty_remaining * b.purchase_price) }} ₽</b>
                <a href="/batches/{{b.id}}/edit" title="Редактировать партию">✎</a></span>
            </div>
          {% else %}
            <div class="muted" style="font-size:12px;margin:4px 0 6px">Партий нет — нажми «+», чтобы добавить первую.</div>
          {% endfor %}

          <div id="bform{{p.id}}" style="display:none;margin-top:8px">
            <form method="post" action="/products/{{p.id}}/batch" class="batch-form"
                  data-batch-calc data-qty="bq{{p.id}}" data-price="bp{{p.id}}" data-total="bt{{p.id}}">
              <input name="purchase_date" type="date" value="{{today}}" class="full" required>
              <input id="bq{{p.id}}" name="qty" type="number" min="1" placeholder="кол-во" required>
              <input id="bp{{p.id}}" name="price" type="number" step="0.01" placeholder="₽ / ед">
              <input id="bt{{p.id}}" type="number" step="0.01" placeholder="или сумма партии" class="full">
              <input name="supplier" placeholder="поставщик" class="full">
              <div class="batch-hint">Введи цену за единицу или общую сумму — второе посчитается само.</div>
              <button class="btn-sm">Сохранить партию</button>
              <button type="button" class="btn btn-gray btn-sm" onclick="toggleBatch({{p.id}})">Отмена</button>
            </form>
          </div>
        </div>
      </div>

      <div class="prod-actions">
        <a href="/products/{{p.id}}/edit" class="btn btn-gray btn-sm">Изменить</a>
        <form method="post" action="/products/{{p.id}}/delete"
              onsubmit="return confirm('Удалить товар? Партии и история останутся в БД.')">
          <button class="btn-danger btn-sm">Удалить</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
{% endfor %}

{% endif %}
""" + BATCH_CALC_JS + BASE_FOOT

ORDERS_TPL = BASE_HEAD + """
<h1>Заказы</h1>

{% if request.query_params.get('error') == 'empty_order' %}
<div class="card" style="background:rgba(239,68,68,.1);border-color:var(--danger);color:#f87171;padding:14px 20px;margin-bottom:20px;border-radius:12px;">
  <b>Внимание!</b> Нельзя пометить заказ как «Вручён», если в нём нет товаров. Сначала добавьте хотя бы один товар.
</div>
{% endif %}

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Новый заказ</h2>
  <form method="post" action="/orders">
    <div class="row-3">
      <div><label>Дата заказа</label><input name="order_date" type="date" value="{{today}}" required></div>
      <div><label>Источник</label>
        <select name="source">
          <option value="avito">Авито</option>
          <option value="telegram">Telegram</option>
          <option value="vk">ВК</option>
          <option value="call">Звонок</option>
          <option value="other">Другое</option>
        </select></div>
      <div><label>Статус</label>
        <select name="status">
          <option value="collecting">Собирается</option>
          <option value="shipped">Отправлен</option>
          <option value="delivered">Вручён</option>
          <option value="returned">Возвращён</option>
          <option value="cancelled">Отменён</option>
        </select></div>
    </div>

    <div style="margin-top:10px">
      <button type="button" class="btn btn-gray btn-sm" onclick="toggleNewOrderExtras()" id="tbtn-new-order">
        + Дополнительно (покупатель, ссылка, комментарий)
      </button>
    </div>

    <div id="new-order-extras" style="display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
      <div class="row-3">
        <div><label>Имя покупателя</label><input name="customer_name"></div>
        <div><label>Телефон</label><input name="customer_phone"></div>
        <div><label>Ссылка (ВК/Авито/ТГ)</label><input name="customer_link"></div>
      </div>
      <label>Комментарий</label><input name="comment">
    </div>

    <div style="margin-top:16px"><button>Создать заказ</button></div>
  </form>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Список заказов</h2>
  <table>
    <tr>
      <th>
        <a href="/orders?sort=id&dir={{ next_dir if current_sort == 'id' else 'asc' }}" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:4px;">
          <span style="display:inline-flex; flex-direction:column; font-size:9px; line-height:9px;">
            <span style="color:{{ '#fff' if current_sort == 'id' and current_dir == 'asc' else '#3b4d44' }};">↑</span>
            <span style="color:{{ '#fff' if current_sort == 'id' and current_dir == 'desc' else '#3b4d44' }};">↓</span>
          </span>
          №
        </a>
      </th>
      
      <th>
        <a href="/orders?sort=date&dir={{ next_dir if current_sort == 'date' else 'asc' }}" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:4px;">
          <span style="display:inline-flex; flex-direction:column; font-size:9px; line-height:9px;">
            <span style="color:{{ '#fff' if current_sort == 'date' and current_dir == 'asc' else '#3b4d44' }};">↑</span>
            <span style="color:{{ '#fff' if current_sort == 'date' and current_dir == 'desc' else '#3b4d44' }};">↓</span>
          </span>
          Дата
        </a>
      </th>
      
      <th>
        <a href="/orders?sort=source&dir={{ next_dir if current_sort == 'source' else 'asc' }}" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:4px;">
          <span style="display:inline-flex; flex-direction:column; font-size:9px; line-height:9px;">
            <span style="color:{{ '#fff' if current_sort == 'source' and current_dir == 'asc' else '#3b4d44' }};">↑</span>
            <span style="color:{{ '#fff' if current_sort == 'source' and current_dir == 'desc' else '#3b4d44' }};">↓</span>
          </span>
          Источник
        </a>
      </th>
      
      <th>Покупатель</th>
      
      <th>
        <a href="/orders?sort=status&dir={{ next_dir if current_sort == 'status' else 'asc' }}" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:4px;">
          <span style="display:inline-flex; flex-direction:column; font-size:9px; line-height:9px;">
            <span style="color:{{ '#fff' if current_sort == 'status' and current_dir == 'asc' else '#3b4d44' }};">↑</span>
            <span style="color:{{ '#fff' if current_sort == 'status' and current_dir == 'desc' else '#3b4d44' }};">↓</span>
          </span>
          Статус
        </a>
      </th>
      
      <th class="right">Сумма</th>
      
      <th class="right">Скидка</th>
      
      <th class="right">
        <a href="/orders?sort=total&dir={{ next_dir if current_sort == 'total' else 'asc' }}" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:4px; justify-content:flex-end;">
          <span style="display:inline-flex; flex-direction:column; font-size:9px; line-height:9px;">
            <span style="color:{{ '#fff' if current_sort == 'total' and current_dir == 'asc' else '#3b4d44' }};">↑</span>
            <span style="color:{{ '#fff' if current_sort == 'total' and current_dir == 'desc' else '#3b4d44' }};">↓</span>
          </span>
          Итого
        </a>
      </th>
      
      <th>Товары</th><th></th>
    </tr>
    {% for o in orders %}
    <tr>
      <td>{{o.id}}</td>
      <td>{{o.order_date}}</td>
      <td>{{ source_label.get(o.source, o.source or '—') }}</td>
      <td>{{o.customer_name or ''}}
        {% if o.customer_link %}<div class="muted"><a href="{{o.customer_link}}" target="_blank">{{o.customer_link}}</a></div>{% endif %}
        {% if o.customer_phone %}<div class="muted">{{o.customer_phone}}</div>{% endif %}
      </td>
      <td>
        <span class="tag {{ status_tag.get(o.status, 'tag-gray') }}">{{ status_label.get(o.status, o.status) }}</span>
        <form method="post" action="/orders/{{o.id}}/status" class="flex" style="margin-top:6px">
          <select name="status" onchange="this.form.submit()">
            {% for s,l in statuses %}
              <option value="{{s}}" {{'selected' if o.status==s else ''}}>{{l}}</option>
            {% endfor %}
          </select>
        </form>
      </td>
      <td class="right">{{ '%.2f'|format(o.subtotal) }} ₽</td>
      <td class="right">{{ '%.2f'|format(o.discount) }} ₽</td>
      <td class="right"><b>{{ '%.2f'|format(o.total) }} ₽</b></td>
      <td>
        {% for it in o['items'] %}
          <div class="muted">
            {{it.name}} × {{it.qty}} @ {{ '%.2f'|format(it.sale_price) }} ₽
            <a href="/orders/{{o.id}}/items/{{it.id}}/edit" title="Редактировать позицию">✎</a>
            <form method="post" action="/orders/{{o.id}}/items/{{it.id}}/delete"
                  style="display:inline" onsubmit="return confirm('Удалить позицию? Товар вернётся на склад.')">
              <button class="btn-danger btn-sm" style="padding:2px 6px;font-size:11px">×</button>
            </form>
          </div>
        {% endfor %}
      </td>
      <td>
        <div class="flex" style="flex-direction:column;align-items:flex-start;gap:6px">
          <a href="/orders/{{o.id}}/edit" class="btn btn-gray btn-sm">Изменить</a>
          
          <button type="button" class="btn btn-sm" style="padding:4px 10px;font-size:12px" onclick="toggleOrderAdd({{o.id}})" id="tbtn-add-{{o.id}}">+ Товар</button>
          <button type="button" class="btn btn-gray btn-sm" style="padding:4px 10px;font-size:12px" onclick="toggleOrderDiscount({{o.id}})" id="tbtn-disc-{{o.id}}">Скидка</button>

          <div id="add-form-{{o.id}}" style="display:none; margin-top:4px">
            <form method="post" action="/orders/{{o.id}}/items" class="flex" style="flex-wrap:wrap; gap:6px; max-width:220px;">
              <select name="product_id" required style="width:100%; font-size:12px; padding:6px 8px;" 
                      onchange="autofillPrice(this, 'price-input-{{o.id}}')">
                <option value="" data-price="0">— товар —</option>
                {% for p in products %}
                  <option value="{{p.id}}" data-price="{{ p.sale_price }}">{{p.name}} ({{p.qty}})</option>
                {% endfor %}
              </select>
              <div class="flex" style="width:100%; gap:6px;">
                <input id="price-input-{{o.id}}" name="sale_price" type="number" step="0.01" placeholder="цена" style="width:80px; font-size:12px; padding:6px 8px;" required>
                <input name="qty" type="number" min="1" value="1" placeholder="кол-во" style="width:60px; font-size:12px; padding:6px 8px;" required>
                <button class="btn-sm">ОК</button>
              </div>
            </form>
          </div>

          <div id="disc-form-{{o.id}}" style="display:none; margin-top:4px">
            <form method="post" action="/orders/{{o.id}}/discount" class="flex" style="gap:6px;">
              <input name="discount" type="number" step="0.01" placeholder="сумма" value="{{o.discount}}" style="width:80px; font-size:12px; padding:6px 8px;">
              <button class="btn-sm btn-gray">ОК</button>
            </form>
          </div>
          
          <form method="post" action="/orders/{{o.id}}/delete" onsubmit="return confirm('Удалить заказ? Товар вернётся в партии.')">
            <button class="btn-danger btn-sm" style="padding:4px 10px; font-size:12px;">Удалить</button>
          </form>
        </div>
      </td>
    </tr>
    {% endfor %}
  </table>
</div>

<script>
function toggleNewOrderExtras(){
  const d = document.getElementById('new-order-extras');
  const b = document.getElementById('tbtn-new-order');
  const open = d.style.display !== 'none';
  d.style.display = open ? 'none' : 'block';
  b.textContent = open ? '+ Дополнительно (покупатель, ссылка, комментарий)' : '− Скрыть дополнительно';
}

function toggleOrderAdd(oid){
  const d = document.getElementById('add-form-'+oid);
  const b = document.getElementById('tbtn-add-'+oid);
  const open = d.style.display !== 'none';
  d.style.display = open ? 'none' : 'block';
  b.textContent = open ? '+ Товар' : '× Закрыть';
  if(!open) {
    document.getElementById('disc-form-'+oid).style.display = 'none';
    document.getElementById('tbtn-disc-'+oid).textContent = 'Скидка';
  }
}

function toggleOrderDiscount(oid){
  const d = document.getElementById('disc-form-'+oid);
  const b = document.getElementById('tbtn-disc-'+oid);
  const open = d.style.display !== 'none';
  d.style.display = open ? 'none' : 'block';
  b.textContent = open ? 'Скидка' : '× Закрыть';
  if(!open) {
    document.getElementById('add-form-'+oid).style.display = 'none';
    document.getElementById('tbtn-add-'+oid).textContent = '+ Товар';
  }
}

function autofillPrice(selectEl, priceInputId) {
  const selectedOption = selectEl.options[selectEl.selectedIndex];
  if (!selectedOption) return;
  const basePrice = selectedOption.getAttribute('data-price');
  const priceInput = document.getElementById(priceInputId);
  if (basePrice && basePrice !== "0" && priceInput) {
    priceInput.value = basePrice;
  }
}
</script>
""" + BASE_FOOT

DEFECTS_TPL = BASE_HEAD + """
<h1>Брак</h1>
<div class="card">
  <h2 style="margin-top:0;font-size:16px">Списать в брак</h2>
  <form method="post" action="/defects">
    <div class="row-3">
      <div><label>Товар</label>
        <select name="product_id" required>
          <option value="">— выберите —</option>
          {% for p in products %}
            <option value="{{p.id}}">{{p.name}} ({{p.qty}})</option>
          {% endfor %}
        </select></div>
      <div><label>Количество</label><input name="qty" type="number" min="1" required></div>
      <div><label>Причина</label>
        <select name="reason">
          <option value="supplier">Брак поставщика</option>
          <option value="transport">Повреждение при транспортировке</option>
          <option value="return">Возврат от клиента</option>
          <option value="other">Другое</option>
        </select></div>
    </div>
    <div class="row">
      <div><label>Дата</label><input name="defect_date" type="date" value="{{today}}" required></div>
      <div><label>Комментарий</label><input name="comment"></div>
    </div>
    <div style="margin-top:12px"><button>Списать</button></div>
  </form>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">История брака</h2>
  <table>
    <tr><th>Дата</th><th>Товар</th><th class="right">Кол-во</th><th>Причина</th>
        <th class="right">Себестоимость</th><th>Комментарий</th></tr>
    {% for d in defects %}
    <tr>
      <td>{{d.defect_date}}</td>
      <td>{{d.name}}</td>
      <td class="right">{{d.qty}}</td>
      <td>{{ reason_label.get(d.reason, d.reason or '—') }}</td>
      <td class="right">{{ '%.2f'|format(d.cost_total) }} ₽</td>
      <td class="muted">{{d.comment or ''}}</td>
    </tr>
    {% endfor %}
  </table>
</div>
""" + BASE_FOOT

REPORTS_TPL = BASE_HEAD + """
<h1>Отчёты</h1>
<div class="card">
  <h2 style="margin-top:0;font-size:16px">Статистика по дням</h2>
  <p class="muted">График показывает динамику по дням. Таблицы ниже — сводка по месяцам.</p>
  
  <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:16px;">
    <div style="background:#0d1310; border:1px solid var(--line); border-radius:12px; padding:16px;">
      <h3 style="font-size:13px; margin:0 0 12px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px;">Прибыль, выручка и себестоимость</h3>
      <canvas id="finChart" height="200"></canvas>
    </div>
    <div style="background:#0d1310; border:1px solid var(--line); border-radius:12px; padding:16px;">
      <h3 style="font-size:13px; margin:0 0 12px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px;">Продажи по источникам</h3>
      <canvas id="sourceChart" height="200"></canvas>
    </div>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Топ товаров по марже</h2>
  <div style="background:#0d1310; border:1px solid var(--line); border-radius:12px; padding:16px;">
    <canvas id="topChart" height="100"></canvas>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Прибыль по месяцам</h2>
  <table>
    <tr><th>Месяц</th><th class="right">Заказов</th><th class="right">Выручка</th>
        <th class="right">Себестоимость</th><th class="right">Прибыль</th><th class="right">Маржа %</th></tr>
    {% for r in monthly %}
    <tr>
      <td>{{r.month}}</td>
      <td class="right">{{r.orders}}</td>
      <td class="right">{{ '%.2f'|format(r.revenue) }} ₽</td>
      <td class="right">{{ '%.2f'|format(r.cost) }} ₽</td>
      <td class="right"><b>{{ '%.2f'|format(r.profit) }} ₽</b></td>
      <td class="right">{{ '%.1f'|format(r.margin) }}%</td>
    </tr>
    {% endfor %}
  </table>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Продажи по источникам (по месяцам)</h2>
  <table>
    <tr><th>Месяц</th><th class="right">Авито</th><th class="right">Telegram</th>
        <th class="right">ВК</th><th class="right">Звонок</th><th class="right">Другое</th>
        <th class="right">Итого</th></tr>
    {% for r in by_source %}
    <tr>
      <td>{{r.month}}</td>
      <td class="right">{{ '%.2f'|format(r.avito) }} ₽</td>
      <td class="right">{{ '%.2f'|format(r.telegram) }} ₽</td>
      <td class="right">{{ '%.2f'|format(r.vk) }} ₽</td>
      <td class="right">{{ '%.2f'|format(r.call) }} ₽</td>
      <td class="right">{{ '%.2f'|format(r.other) }} ₽</td>
      <td class="right"><b>{{ '%.2f'|format(r.total) }} ₽</b></td>
    </tr>
    {% endfor %}
  </table>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:16px">Сколько чего было продано</h2>
  <table>
    <tr>
      <th>Товар</th>
      <th class="right">Продано (шт)</th>
      <th class="right">Выручка</th>
    </tr>
    {% for r in sold_items %}
    <tr>
      <td>{{r.name}}</td>
      <td class="right">{{r.qty_sold}}</td>
      <td class="right"><b>{{ '%.2f'|format(r.total_revenue) }} ₽</b></td>
    </tr>
    {% else %}
    <tr><td colspan="3" class="muted" style="text-align:center;">Пока нет вручённых заказов</td></tr>
    {% endfor %}
  </table>
</div>

<script>
  // Общие настройки
  Chart.defaults.color = '#8fa39a';
  Chart.defaults.borderColor = '#233029';
  Chart.defaults.font.family = 'ui-sans-serif, system-ui, sans-serif';
  Chart.defaults.elements.point.backgroundColor = '#0d1310'; 
  Chart.defaults.elements.point.borderColor = 'transparent'; 

  const dailyLabels = {{ daily_labels | safe }};
  const dailyRevenue = {{ daily_revenue | safe }};
  const dailyCost = {{ daily_cost | safe }};
  const dailyProfit = {{ daily_profit | safe }};

  const sourceLabels = {{ source_labels | safe }};
  const sourceAvito = {{ source_avito | safe }};
  const sourceTg = {{ source_telegram | safe }};
  const sourceVk = {{ source_vk | safe }};
  const sourceCall = {{ source_call | safe }};
  const sourceOther = {{ source_other | safe }};

  const topNames = {{ top_names | safe }};
  const topProfits = {{ top_profits | safe }};

  // 1. График финансов
  new Chart(document.getElementById('finChart'), {
    type: 'line',
    data: {
      labels: dailyLabels,
      datasets: [
        { 
          label: 'Выручка', data: dailyRevenue, borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,.1)', 
          tension: 0, fill: true,
          pointBackgroundColor: '#4ade80', pointBorderColor: '#0d1310',
          pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#4ade80', pointHoverRadius: 6
        },
        { 
          label: 'Себестоимость', data: dailyCost, borderColor: '#f87171', backgroundColor: 'transparent', 
          tension: 0,
          pointBackgroundColor: '#f87171', pointBorderColor: '#0d1310',
          pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#f87171', pointHoverRadius: 6
        },
        { 
          label: 'Прибыль', data: dailyProfit, borderColor: '#60a5fa', backgroundColor: 'transparent', 
          tension: 0,
          pointBackgroundColor: '#60a5fa', pointBorderColor: '#0d1310',
          pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#60a5fa', pointHoverRadius: 6
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom' } },
      scales: { y: { beginAtZero: true, grid: { color: '#233029' } }, x: { grid: { display: false } } }
    }
  });

  // 2. График по источникам
  new Chart(document.getElementById('sourceChart'), {
    type: 'line',
    data: {
      labels: sourceLabels,
      datasets: [
        { label: 'Авито', data: sourceAvito, borderColor: '#4ade80', tension: 0, 
          pointBackgroundColor: '#4ade80', pointBorderColor: '#0d1310', pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#4ade80', pointHoverRadius: 6 },
        { label: 'Telegram', data: sourceTg, borderColor: '#38bdf8', tension: 0, 
          pointBackgroundColor: '#38bdf8', pointBorderColor: '#0d1310', pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#38bdf8', pointHoverRadius: 6 },
        { label: 'ВК', data: sourceVk, borderColor: '#a78bfa', tension: 0, 
          pointBackgroundColor: '#a78bfa', pointBorderColor: '#0d1310', pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#a78bfa', pointHoverRadius: 6 },
        { label: 'Звонок', data: sourceCall, borderColor: '#facc15', tension: 0, 
          pointBackgroundColor: '#facc15', pointBorderColor: '#0d1310', pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#facc15', pointHoverRadius: 6 },
        { label: 'Другое', data: sourceOther, borderColor: '#ef4444', tension: 0, 
          pointBackgroundColor: '#ef4444', pointBorderColor: '#0d1310', pointHoverBackgroundColor: '#fff', pointHoverBorderColor: '#ef4444', pointHoverRadius: 6 }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom' } },
      scales: { y: { beginAtZero: true, grid: { color: '#233029' } }, x: { grid: { display: false } } }
    }
  });

  // 3. Горизонтальная диаграмма топ-товаров
  new Chart(document.getElementById('topChart'), {
    type: 'bar',
    data: {
      labels: topNames,
      datasets: [{
        label: 'Прибыль (₽)',
        data: topProfits,
        backgroundColor: 'rgba(34,197,94,.8)',
        hoverBackgroundColor: '#4ade80',
        borderRadius: 4
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, grid: { color: '#233029' } }, y: { grid: { display: false } } }
    }
  });
</script>
""" + BASE_FOOT

AUDIT_TPL = BASE_HEAD + """
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
  <h1 style="margin:0;">Сверка склада</h1>
  <div style="display:flex; gap:10px;">
    <form method="post" action="/audit/fix" onsubmit="return confirm('Программа пересчитает остатки на складе по формуле Закуплено − Продано − Брак. Продолжить?')">
      <button class="btn" style="background:#2a1616; color:#f87171; border:1px solid #4a1c1c; box-shadow:none;">🛠 Исправить расхождения</button>
    </form>
    <a href="/" class="btn btn-gray">← Назад на склад</a>
  </div>
</div>

<div class="card">
  <p class="muted">Проверка: <b>Закуплено − Продано − Брак = Остаток</b>. Если есть расхождения (красные строки), значит остатки в базе не совпадают с реальным движением товаров.</p>
  
  <table>
    <tr>
      <th>Товар</th>
      <th class="right">Закуплено</th>
      <th class="right">Продано</th>
      <th class="right">Брак</th>
      <th class="right">Должно быть</th>
      <th class="right">Факт в базе</th>
      <th class="right">Расхождение</th>
    </tr>
    {% for r in audit_data %}
    <tr {% if r.diff != 0 %}style="background:rgba(239,68,68,.08);"{% endif %}>
      <td><b>{{r.name}}</b></td>
      <td class="right">{{r.purchased}}</td>
      <td class="right">{{r.sold}}</td>
      <td class="right">{{r.defects}}</td>
      <td class="right">{{r.should_be}}</td>
      <td class="right">{{r.actual}}</td>
      <td class="right">
        {% if r.diff == 0 %}
          <span style="color:#4ade80;">0</span>
        {% else %}
          <b style="color:#f87171;">{{ r.diff }}</b>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
</div>
""" + BASE_FOOT

# -------------------- РЕДАКТИРОВАНИЕ --------------------

EDIT_PRODUCT_TPL = BASE_HEAD + """
<h1>Редактировать товар</h1>
<div class="card">
  <form method="post" action="/products/{{p.id}}/edit" enctype="multipart/form-data">
    <div class="row-3">
      <div><label>Название</label><input name="name" value="{{p.name}}" required></div>
      <div><label>Категория</label>
        <select name="category">
          {% for c in ['прицелы','фонари','планки','рукоятки','глушители','другое'] %}
            <option {{'selected' if p.category==c else ''}}>{{c}}</option>
          {% endfor %}
        </select></div>
      <div><label>Артикул / SKU</label><input name="sku" value="{{p.sku or ''}}"></div>
    </div>
    <div class="row-3">
      <div><label>Цена продажи (базовая)</label>
        <input name="sale_price" type="number" step="0.01" value="{{p.sale_price}}"></div>
      <div><label>Картинка (оставь пустым — не менять)</label>
        <input name="image" type="file" accept="image/*">
        {% if p.image_url %}<div class="muted" style="margin-top:6px">
          <img src="{{p.image_url}}" class="thumb"> текущая
        </div>{% endif %}
      </div>
      <div><label>Описание</label><input name="description" value="{{p.description or ''}}"></div>
    </div>
    <div class="flex" style="margin-top:14px">
      <button>Сохранить</button>
      <a href="/" class="btn btn-gray">Отмена</a>
    </div>
  </form>
</div>
""" + BASE_FOOT

EDIT_BATCH_TPL = BASE_HEAD + """
<h1>Редактировать партию</h1>
<div class="card">
  <p class="muted">Товар: <b>{{p.name}}</b></p>
  <p class="muted">Продано из партии: {{sold}} шт. Минимальное количество = {{sold}}.</p>
  <form method="post" action="/batches/{{b.id}}/edit"
        data-batch-calc data-qty="eq" data-price="ep" data-total="et">
    <div class="row-3">
      <div><label>Дата закупки</label>
        <input name="purchase_date" type="date" value="{{b.purchase_date}}" required></div>
      <div><label>Количество (изначальное)</label>
        <input id="eq" name="qty_initial" type="number" min="{{sold}}" value="{{b.qty_initial}}" required></div>
      <div><label>Цена закупки за ед.</label>
        <input id="ep" name="purchase_price" type="number" step="0.01" value="{{b.purchase_price}}" required></div>
    </div>
    <div class="row">
      <div><label>Сумма партии (авто / можно ввести)</label>
        <input id="et" type="number" step="0.01" value="{{ '%.2f'|format(b.qty_initial * b.purchase_price) }}"></div>
      <div><label>Поставщик</label><input name="supplier" value="{{b.supplier or ''}}"></div>
    </div>
    <div class="row" style="grid-template-columns:1fr">
      <div><label>Комментарий</label><input name="comment" value="{{b.comment or ''}}"></div>
    </div>
    <div class="flex" style="margin-top:14px">
      <button>Сохранить</button>
      <a href="/" class="btn btn-gray">Отмена</a>
    </div>
  </form>
</div>
""" + BATCH_CALC_JS + BASE_FOOT

EDIT_ORDER_TPL = BASE_HEAD + """
<h1>Редактировать заказ #{{o.id}}</h1>
<div class="card">
  <form method="post" action="/orders/{{o.id}}/edit">
    <div class="row-3">
      <div><label>Дата заказа</label>
        <input name="order_date" type="date" value="{{o.order_date}}" required></div>
      <div><label>Источник</label>
        <select name="source">
          {% for s,l in source_label.items() %}
            <option value="{{s}}" {{'selected' if o.source==s else ''}}>{{l}}</option>
          {% endfor %}
        </select></div>
      <div><label>Статус</label>
        <select name="status">
          {% for s,l in statuses %}
            <option value="{{s}}" {{'selected' if o.status==s else ''}}>{{l}}</option>
          {% endfor %}
        </select></div>
    </div>
    <div class="row-3">
      <div><label>Имя покупателя</label><input name="customer_name" value="{{o.customer_name or ''}}"></div>
      <div><label>Телефон</label><input name="customer_phone" value="{{o.customer_phone or ''}}"></div>
      <div><label>Ссылка (ВК/Авито)</label><input name="customer_link" value="{{o.customer_link or ''}}"></div>
    </div>
    <label>Комментарий</label><input name="comment" value="{{o.comment or ''}}">
    <div class="flex" style="margin-top:14px">
      <button>Сохранить</button>
      <a href="/orders" class="btn btn-gray">Отмена</a>
    </div>
  </form>
</div>
""" + BASE_FOOT

EDIT_ITEM_TPL = BASE_HEAD + """
<h1>Редактировать позицию заказа #{{o.id}}</h1>
<div class="card">
  <p class="muted">Товар: <b>{{it.name}}</b></p>
  <p class="muted">Доступно на складе (без учёта этой позиции): {{available}} шт.</p>
  <form method="post" action="/orders/{{o.id}}/items/{{it.id}}/edit">
    <div class="row">
      <div><label>Количество</label>
        <input name="qty" type="number" min="1" max="{{it.qty + available}}" value="{{it.qty}}" required></div>
      <div><label>Цена продажи за ед.</label>
        <input name="sale_price" type="number" step="0.01" value="{{it.sale_price}}" required></div>
    </div>
    <div class="flex" style="margin-top:14px">
      <button>Сохранить</button>
      <a href="/orders" class="btn btn-gray">Отмена</a>
    </div>
  </form>
</div>
""" + BASE_FOOT

# Инициализация Jinja2
_env = jinja2.Environment(loader=DictLoader({
    "index.html": INDEX_TPL,
    "orders.html": ORDERS_TPL,
    "defects.html": DEFECTS_TPL,
    "reports.html": REPORTS_TPL,
    "audit.html": AUDIT_TPL,
    "edit_product.html": EDIT_PRODUCT_TPL,
    "edit_batch.html": EDIT_BATCH_TPL,
    "edit_order.html": EDIT_ORDER_TPL,
    "edit_item.html": EDIT_ITEM_TPL,
}))
templates = Jinja2Templates(env=_env)

# ============================================================
# APP
# ============================================================

app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def load_products(conn):
    products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    out = []
    for p in products:
        batches = conn.execute(
            "SELECT * FROM batches WHERE product_id=? AND qty_remaining>0 ORDER BY purchase_date, id",
            (p["id"],),
        ).fetchall()
        qty = sum(b["qty_remaining"] for b in batches)
        stock_value = sum(b["qty_remaining"] * b["purchase_price"] for b in batches)
        avg_purchase = (stock_value / qty) if qty > 0 else 0
        last = conn.execute(
            "SELECT purchase_price FROM batches WHERE product_id=? "
            "ORDER BY purchase_date DESC, id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        last_purchase = last["purchase_price"] if last else 0
        out.append({
            "id": p["id"], "name": p["name"], "category": p["category"], "sku": p["sku"],
            "sale_price": p["sale_price"], "image_url": p["image_url"],
            "description": p["description"],
            "qty": qty, "stock_value": stock_value,
            "avg_purchase": avg_purchase, "last_purchase": last_purchase,
            "batches": [dict(b) for b in batches],
        })
    return out


def load_products_grouped(conn):
    products = load_products(conn)
    groups = {}
    for p in products:
        cat = (p["category"] or "без категории").strip()
        groups.setdefault(cat, []).append(p)
    for cat in groups:
        groups[cat].sort(key=lambda x: x["sale_price"], reverse=True)
    ordered = sorted(groups.items(), key=lambda kv: sum(p["stock_value"] for p in kv[1]), reverse=True)
    result = [{
        "name": cat, "total_stock_value": sum(p["stock_value"] for p in items),
        "total_qty": sum(p["qty"] for p in items), "count": len(items), "products": items,
    } for cat, items in ordered]
    totals = {
        "stock_value": sum(g["total_stock_value"] for g in result),
        "qty": sum(g["total_qty"] for g in result),
        "products": sum(g["count"] for g in result),
        "categories": len(result),
    }
    return result, totals


def load_orders(conn):
    orders = conn.execute("SELECT * FROM orders ORDER BY order_date DESC, id DESC").fetchall()
    out = []
    for o in orders:
        items = conn.execute(
            "SELECT oi.*, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id "
            "WHERE oi.order_id=? ORDER BY oi.id", (o["id"],),
        ).fetchall()
        out.append({**dict(o), "items": [dict(i) for i in items]})
    return out


def recalc_order_total(conn, order_id: int):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty*sale_price),0) AS s FROM order_items WHERE order_id=?",
        (order_id,),
    ).fetchone()
    subtotal = row["s"]
    d = conn.execute("SELECT discount FROM orders WHERE id=?", (order_id,)).fetchone()
    discount = d["discount"] if d else 0
    conn.execute(
        "UPDATE orders SET subtotal=?, total=? WHERE id=?",
        (subtotal, max(subtotal - discount, 0), order_id),
    )


def available_stock(conn, product_id: int, exclude_order_item_id: int | None = None) -> int:
    total = conn.execute(
        "SELECT COALESCE(SUM(qty_remaining),0) AS s FROM batches WHERE product_id=?", (product_id,),
    ).fetchone()["s"]
    if exclude_order_item_id is not None:
        extra = conn.execute("SELECT qty FROM order_items WHERE id=?", (exclude_order_item_id,)).fetchone()
        if extra: total += extra["qty"]
    return total


# ---------------- СКЛАД ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db() as conn:
        groups, totals = load_products_grouped(conn)
    return templates.TemplateResponse(request, "index.html", {
        "groups": groups, "totals": totals, "today": str(date.today()),
    })


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    with db() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        audit_data = []
        for p in products:
            pid = p["id"]
            purchased = conn.execute("SELECT COALESCE(SUM(qty_initial), 0) FROM batches WHERE product_id=?", (pid,)).fetchone()[0]
            sold = conn.execute("""
                SELECT COALESCE(SUM(oi.qty), 0) 
                FROM order_items oi 
                JOIN orders o ON o.id = oi.order_id 
                WHERE oi.product_id = ? AND o.status IN ('collecting', 'shipped', 'delivered')
            """, (pid,)).fetchone()[0]
            defect_qty = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM defects WHERE product_id=?", (pid,)).fetchone()[0]
            should_be = purchased - sold - defect_qty
            actual = conn.execute("SELECT COALESCE(SUM(qty_remaining), 0) FROM batches WHERE product_id=?", (pid,)).fetchone()[0]
            diff = actual - should_be
            audit_data.append({
                "id": pid, "name": p["name"], "purchased": purchased, "sold": sold,
                "defects": defect_qty, "should_be": should_be, "actual": actual, "diff": diff
            })
    return templates.TemplateResponse(request, "audit.html", {"audit_data": audit_data})

@app.post("/audit/fix")
def audit_fix():
    with db() as conn:
        products = conn.execute("SELECT * FROM products").fetchall()
        
        for p in products:
            pid = p["id"]
            
            # Считаем правильный остаток
            purchased = conn.execute("SELECT COALESCE(SUM(qty_initial), 0) FROM batches WHERE product_id=?", (pid,)).fetchone()[0]
            sold = conn.execute("""
                SELECT COALESCE(SUM(oi.qty), 0) 
                FROM order_items oi 
                JOIN orders o ON o.id = oi.order_id 
                WHERE oi.product_id = ? AND o.status IN ('collecting', 'shipped', 'delivered')
            """, (pid,)).fetchone()[0]
            defect_qty = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM defects WHERE product_id=?", (pid,)).fetchone()[0]
            
            should_be = purchased - sold - defect_qty
            actual = conn.execute("SELECT COALESCE(SUM(qty_remaining), 0) FROM batches WHERE product_id=?", (pid,)).fetchone()[0]
            
            diff = actual - should_be
            
            if diff != 0:
                # Если не хватает (diff < 0) - нужно добавить остатки в партии
                if diff < 0:
                    # Берем самую свежую партию (последнюю купленную) и накидываем туда
                    batch = conn.execute("SELECT id FROM batches WHERE product_id=? ORDER BY purchase_date DESC, id DESC LIMIT 1", (pid,)).fetchone()
                    if batch:
                        conn.execute("UPDATE batches SET qty_remaining = qty_remaining + ? WHERE id=?", (abs(diff), batch["id"]))
                        conn.execute("INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date) VALUES (?,?,?,?,?)", 
                                     (pid, batch["id"], "fix", abs(diff), str(date.today())))
                    else:
                        # Если партий нет вообще, создаем нулевую партию
                        cur = conn.execute("INSERT INTO batches (product_id, purchase_date, qty_initial, qty_remaining, purchase_price) VALUES (?,?,?,?,?)",
                                     (pid, str(date.today()), 0, abs(diff), 0))
                        conn.execute("INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date) VALUES (?,?,?,?,?)", 
                                     (pid, cur.lastrowid, "fix", abs(diff), str(date.today())))
                
                # Если лишнее (diff > 0) - нужно списать из партий (начиная со старых)
                elif diff > 0:
                    remaining_to_remove = diff
                    batches = conn.execute("SELECT * FROM batches WHERE product_id=? AND qty_remaining>0 ORDER BY purchase_date, id", (pid,)).fetchall()
                    for b in batches:
                        if remaining_to_remove == 0: break
                        take = min(b["qty_remaining"], remaining_to_remove)
                        conn.execute("UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id=?", (take, b["id"]))
                        conn.execute("INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date) VALUES (?,?,?,?,?)", 
                                     (pid, b["id"], "fix", -take, str(date.today())))
                        remaining_to_remove -= take

    return RedirectResponse("/audit", status_code=303)

@app.post("/products")
async def create_product(
    name: str = Form(...), category: str = Form(""), sku: str = Form(""),
    sale_price: float = Form(0), description: str = Form(""), image: UploadFile | None = File(None),
):
    image_url = ""
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        fname = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR, fname), "wb") as f: shutil.copyfileobj(image.file, f)
        image_url = f"/uploads/{fname}"
    with db() as conn:
        conn.execute(
            "INSERT INTO products (name, category, sku, sale_price, image_url, description) VALUES (?,?,?,?,?,?)",
            (name, category, sku, sale_price, image_url, description),
        )
    return RedirectResponse("/", status_code=303)


@app.post("/products/{pid}/batch")
def add_batch(pid: int, purchase_date: str = Form(...), qty: int = Form(...),
              price: float | None = Form(None), supplier: str = Form("")):
    if price is None: raise HTTPException(400, "Укажите цену за единицу или сумму партии")
    with db() as conn:
        conn.execute(
            "INSERT INTO batches (product_id, purchase_date, qty_initial, qty_remaining, purchase_price, supplier) VALUES (?,?,?,?,?,?)",
            (pid, purchase_date, qty, qty, price, supplier),
        )
        conn.execute("INSERT INTO stock_movements (product_id, type, qty, movement_date) VALUES (?,?,?,?)", (pid, "in", qty, purchase_date))
    return RedirectResponse("/", status_code=303)


@app.post("/products/{pid}/delete")
def delete_product(pid: int):
    with db() as conn: conn.execute("DELETE FROM products WHERE id=?", (pid,))
    return RedirectResponse("/", status_code=303)


@app.get("/products/{pid}/edit", response_class=HTMLResponse)
def edit_product_page(request: Request, pid: int):
    with db() as conn: p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p: raise HTTPException(404, "Товар не найден")
    return templates.TemplateResponse(request, "edit_product.html", {"p": dict(p)})


@app.post("/products/{pid}/edit")
async def edit_product(
    pid: int, name: str = Form(...), category: str = Form(""), sku: str = Form(""),
    sale_price: float = Form(0), description: str = Form(""), image: UploadFile | None = File(None),
):
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p: raise HTTPException(404, "Товар не найден")
        image_url = p["image_url"]
        if image and image.filename:
            ext = os.path.splitext(image.filename)[1].lower()
            fname = f"{uuid.uuid4().hex}{ext}"
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f: shutil.copyfileobj(image.file, f)
            image_url = f"/uploads/{fname}"
        conn.execute(
            "UPDATE products SET name=?, category=?, sku=?, sale_price=?, image_url=?, description=? WHERE id=?",
            (name, category, sku, sale_price, image_url, description, pid),
        )
    return RedirectResponse("/", status_code=303)


# ---------------- ПАРТИИ ----------------

@app.get("/batches/{bid}/edit", response_class=HTMLResponse)
def edit_batch_page(request: Request, bid: int):
    with db() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not b: raise HTTPException(404, "Партия не найдена")
        p = conn.execute("SELECT * FROM products WHERE id=?", (b["product_id"],)).fetchone()
        sold = b["qty_initial"] - b["qty_remaining"]
    return templates.TemplateResponse(request, "edit_batch.html", {"b": dict(b), "p": dict(p), "sold": sold})


@app.post("/batches/{bid}/edit")
def edit_batch(bid: int, purchase_date: str = Form(...), qty_initial: int = Form(...),
               purchase_price: float = Form(...), supplier: str = Form(""), comment: str = Form("")):
    with db() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not b: raise HTTPException(404, "Партия не найдена")
        sold = b["qty_initial"] - b["qty_remaining"]
        if qty_initial < sold: raise HTTPException(400, f"Нельзя установить количество меньше проданного ({sold} шт.)")
        conn.execute(
            "UPDATE batches SET purchase_date=?, qty_initial=?, qty_remaining=?, purchase_price=?, supplier=?, comment=? WHERE id=?",
            (purchase_date, qty_initial, qty_initial - sold, purchase_price, supplier, comment, bid),
        )
    return RedirectResponse("/", status_code=303)


# ---------------- ЗАКАЗЫ ----------------

@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, sort: str = "date", dir: str = "desc"):
    with db() as conn:
        products = load_products(conn)
        
        sort_field = "o.order_date"
        if sort == "source":
            sort_field = "o.source"
        elif sort == "status":
            sort_field = """
                CASE o.status 
                    WHEN 'collecting' THEN 1 
                    WHEN 'shipped' THEN 2 
                    WHEN 'delivered' THEN 3 
                    WHEN 'returned' THEN 4 
                    WHEN 'cancelled' THEN 5 
                    ELSE 6 
                END
            """
        elif sort == "total":
            sort_field = "o.total"
        elif sort == "id":
            sort_field = "o.id"

        sort_dir = "ASC" if dir == "asc" else "DESC"

        orders_raw = conn.execute(f"""
            SELECT * FROM orders o ORDER BY {sort_field} {sort_dir}, o.id DESC
        """).fetchall()
        
        orders = []
        for o in orders_raw:
            items = conn.execute(
                "SELECT oi.*, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id "
                "WHERE oi.order_id=? ORDER BY oi.id", (o["id"],),
            ).fetchall()
            orders.append({**dict(o), "items": [dict(i) for i in items]})

    current_sort = sort
    current_dir = dir
    next_dir = "desc" if dir == "asc" else "asc"

    return templates.TemplateResponse(request, "orders.html", {
        "products": products, "orders": orders,
        "source_label": SOURCE_LABEL, "statuses": STATUSES,
        "status_label": STATUS_LABEL, "status_tag": STATUS_TAG,
        "today": str(date.today()),
        "current_sort": current_sort,
        "current_dir": current_dir,
        "next_dir": next_dir,
    })


@app.post("/orders")
def create_order(order_date: str = Form(...), source: str = Form("other"), status: str = Form("collecting"),
                 customer_name: str = Form(""), customer_phone: str = Form(""), customer_link: str = Form(""), comment: str = Form("")):
    if status == "delivered":
        raise HTTPException(400, "Нельзя создать заказ сразу со статусом 'Вручён'. Сначала добавьте товары, затем смените статус.")
    
    with db() as conn:
        conn.execute(
            "INSERT INTO orders (order_date, source, status, customer_name, customer_phone, customer_link, comment) VALUES (?,?,?,?,?,?,?)",
            (order_date, source, status, customer_name, customer_phone, customer_link, comment),
        )
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{oid}/items")
def add_order_item(oid: int, product_id: int = Form(...), qty: int = Form(...), sale_price: float = Form(...)):
    with db() as conn:
        order = conn.execute("SELECT status, delivered_date FROM orders WHERE id=?", (oid,)).fetchone()
        if not order: raise HTTPException(404, "Заказ не найден")
        
        movement_date = order["delivered_date"] if order["status"] == "delivered" and order["delivered_date"] else str(date.today())

        cur = conn.execute(
            "INSERT INTO order_items (order_id, product_id, qty, sale_price, cost_total) VALUES (?,?,?,?,0)",
            (oid, product_id, qty, sale_price),
        )
        item_id = cur.lastrowid
        cost = write_off_fifo(conn, product_id, qty, item_id, oid, movement_date)
        conn.execute("UPDATE order_items SET cost_total=? WHERE id=?", (cost, item_id))
        recalc_order_total(conn, oid)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{oid}/discount")
def set_discount(oid: int, discount: float = Form(0)):
    with db() as conn:
        conn.execute("UPDATE orders SET discount=? WHERE id=?", (discount, oid))
        recalc_order_total(conn, oid)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{oid}/status")
def set_status(oid: int, status: str = Form(...)):
    with db() as conn:
        old = conn.execute("SELECT status, delivered_date FROM orders WHERE id=?", (oid,)).fetchone()
        if not old: raise HTTPException(404, "Заказ не найден")
        old_status = old["status"]
        delivered_date = old["delivered_date"]

        if status == "delivered":
            items_count = conn.execute("SELECT COUNT(*) as c FROM order_items WHERE order_id=?", (oid,)).fetchone()["c"]
            if items_count == 0:
                return RedirectResponse("/orders?error=empty_order", status_code=303)
            
            if not delivered_date:
                delivered_date = str(date.today())
            
            if old_status in ("returned", "cancelled", "collecting", "shipped"):
                conn.execute("DELETE FROM order_item_batches WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)", (oid,))
                items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
                for it in items:
                    cost = write_off_fifo(conn, it["product_id"], it["qty"], it["id"], oid, delivered_date)
                    conn.execute("UPDATE order_items SET cost_total=? WHERE id=?", (cost, it["id"]))

        if status in ("returned", "cancelled") and old_status == "delivered":
            return_to_batches(conn, oid)

        conn.execute("UPDATE orders SET status=?, delivered_date=? WHERE id=?", (status, delivered_date, oid))
    return RedirectResponse("/orders", status_code=303)


@app.get("/orders/{oid}/edit", response_class=HTMLResponse)
def edit_order_page(request: Request, oid: int):
    with db() as conn: o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: raise HTTPException(404, "Заказ не найден")
    return templates.TemplateResponse(request, "edit_order.html", {
        "o": dict(o), "source_label": SOURCE_LABEL, "statuses": STATUSES,
    })


@app.post("/orders/{oid}/edit")
def edit_order(
    oid: int, order_date: str = Form(...), source: str = Form("other"), status: str = Form("collecting"),
    customer_name: str = Form(""), customer_phone: str = Form(""), customer_link: str = Form(""), comment: str = Form(""),
):
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o: raise HTTPException(404, "Заказ не найден")
        old_status = o["status"]
        delivered_date = o["delivered_date"]

        if status == "delivered":
            items_count = conn.execute("SELECT COUNT(*) as c FROM order_items WHERE order_id=?", (oid,)).fetchone()["c"]
            if items_count == 0:
                return RedirectResponse("/orders?error=empty_order", status_code=303)
            
            if not delivered_date:
                delivered_date = str(date.today())
                
            if old_status in ("returned", "cancelled", "collecting", "shipped"):
                conn.execute("DELETE FROM order_item_batches WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)", (oid,))
                items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
                for it in items:
                    cost = write_off_fifo(conn, it["product_id"], it["qty"], it["id"], oid, delivered_date)
                    conn.execute("UPDATE order_items SET cost_total=? WHERE id=?", (cost, it["id"]))

        if status in ("returned", "cancelled") and old_status == "delivered":
            return_to_batches(conn, oid)

        conn.execute(
            "UPDATE orders SET order_date=?, source=?, status=?, customer_name=?, customer_phone=?, customer_link=?, comment=?, delivered_date=? WHERE id=?",
            (order_date, source, status, customer_name, customer_phone, customer_link, comment, delivered_date, oid),
        )
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{oid}/delete")
def delete_order(oid: int):
    with db() as conn:
        o = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        if o and o["status"] in ("collecting", "shipped", "delivered"):
            return_to_batches(conn, oid)
        conn.execute("DELETE FROM order_item_batches WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)", (oid,))
        conn.execute("DELETE FROM order_items WHERE order_id=?", (oid,))
        conn.execute("DELETE FROM orders WHERE id=?", (oid,))
    return RedirectResponse("/orders", status_code=303)


# ---------------- ПОЗИЦИИ ЗАКАЗА ----------------

@app.get("/orders/{oid}/items/{iid}/edit", response_class=HTMLResponse)
def edit_item_page(request: Request, oid: int, iid: int):
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid)).fetchone()
        it = conn.execute("SELECT oi.*, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.id=? AND oi.order_id=?", (iid, oid)).fetchone()
        if not o or not it: raise HTTPException(404, "Позиция не найдена")
        avail = available_stock(conn, it["product_id"], exclude_order_item_id=iid)
    return templates.TemplateResponse(request, "edit_item.html", {"o": dict(o), "it": dict(it), "available": avail})


@app.post("/orders/{oid}/items/{iid}/edit")
def edit_item(oid: int, iid: int, qty: int = Form(...), sale_price: float = Form(...)):
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        it = conn.execute("SELECT * FROM order_items WHERE id=? AND order_id=?", (iid, oid)).fetchone()
        if not o or not it: raise HTTPException(404, "Позиция не найдена")
        
        movement_date = o["delivered_date"] if o["status"] == "delivered" and o["delivered_date"] else str(date.today())

        breakdown = conn.execute("SELECT * FROM order_item_batches WHERE order_item_id=?", (iid,)).fetchall()
        for bd in breakdown:
            conn.execute("UPDATE batches SET qty_remaining=qty_remaining+? WHERE id=?", (bd["qty"], bd["batch_id"]))
        conn.execute("DELETE FROM order_item_batches WHERE order_item_id=?", (iid,))

        cost = write_off_fifo(conn, it["product_id"], qty, iid, oid, movement_date)
        conn.execute("UPDATE order_items SET qty=?, sale_price=?, cost_total=? WHERE id=?", (qty, sale_price, cost, iid))
        recalc_order_total(conn, oid)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{oid}/items/{iid}/delete")
def delete_item(oid: int, iid: int):
    with db() as conn:
        it = conn.execute("SELECT * FROM order_items WHERE id=? AND order_id=?", (iid, oid)).fetchone()
        if not it: raise HTTPException(404, "Позиция не найдена")
        breakdown = conn.execute("SELECT * FROM order_item_batches WHERE order_item_id=?", (iid,)).fetchall()
        for bd in breakdown:
            conn.execute("UPDATE batches SET qty_remaining=qty_remaining+? WHERE id=?", (bd["qty"], bd["batch_id"]))
        conn.execute("DELETE FROM order_item_batches WHERE order_item_id=?", (iid,))
        conn.execute("DELETE FROM order_items WHERE id=?", (iid,))
        recalc_order_total(conn, oid)
    return RedirectResponse("/orders", status_code=303)


# ---------------- БРАК ----------------

@app.get("/defects", response_class=HTMLResponse)
def defects_page(request: Request):
    with db() as conn:
        products = load_products(conn)
        defects = conn.execute("SELECT d.*, p.name FROM defects d JOIN products p ON p.id=d.product_id ORDER BY d.defect_date DESC, d.id DESC").fetchall()
    return templates.TemplateResponse(request, "defects.html", {
        "products": products, "defects": [dict(d) for d in defects],
        "reason_label": REASON_LABEL, "today": str(date.today()),
    })


@app.post("/defects")
def create_defect(product_id: int = Form(...), qty: int = Form(...), reason: str = Form("other"), defect_date: str = Form(...), comment: str = Form("")):
    with db() as conn:
        rows = conn.execute("SELECT * FROM batches WHERE product_id=? AND qty_remaining>0 ORDER BY purchase_date, id", (product_id,)).fetchall()
        available = sum(r["qty_remaining"] for r in rows)
        if available < qty: raise HTTPException(400, f"Недостаточно товара: нужно {qty}, есть {available}")
        remaining = qty
        cost_total = 0.0
        cur = conn.execute("INSERT INTO defects (product_id, qty, reason, defect_date, cost_total, comment) VALUES (?,?,?,?,0,?)", (product_id, qty, reason, defect_date, comment))
        defect_id = cur.lastrowid
        for b in rows:
            if remaining == 0: break
            take = min(b["qty_remaining"], remaining)
            conn.execute("UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?", (take, b["id"]))
            conn.execute("INSERT INTO stock_movements (product_id, batch_id, type, qty, movement_date, ref_defect_id) VALUES (?,?,?,?,?,?)", (product_id, b["id"], "defect", -take, defect_date, defect_id))
            cost_total += take * b["purchase_price"]
            remaining -= take
        conn.execute("UPDATE defects SET cost_total=? WHERE id=?", (cost_total, defect_id))
    return RedirectResponse("/defects", status_code=303)


# ---------------- ОТЧЁТЫ ----------------

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    from datetime import datetime, timedelta
    
    with db() as conn:
        # --- Таблицы по месяцам ---
        monthly_rev = conn.execute("""
            SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS orders, SUM(total) AS revenue 
            FROM orders WHERE status='delivered' GROUP BY month
        """).fetchall()
        monthly_cost = conn.execute("""
            SELECT strftime('%Y-%m', o.order_date) AS month, SUM(oi.cost_total) AS cost 
            FROM orders o JOIN order_items oi ON oi.order_id = o.id 
            WHERE o.status='delivered' GROUP BY month
        """).fetchall()
        cost_map = {r["month"]: r["cost"] or 0 for r in monthly_cost}
        
        monthly = []
        for r in monthly_rev:
            m = r["month"]
            rev = r["revenue"] or 0
            c = cost_map.get(m, 0)
            monthly.append({
                "month": m, "orders": r["orders"], "revenue": rev, "cost": c,
                "profit": rev - c, "margin": (rev - c) / rev * 100 if rev else 0,
            })
        monthly.sort(key=lambda x: x["month"], reverse=True)

        raw_source = conn.execute("""
            SELECT strftime('%Y-%m', order_date) AS month, source, SUM(total) AS revenue 
            FROM orders WHERE status='delivered' GROUP BY month, source
        """).fetchall()
        by_source_map = {}
        for r in raw_source:
            m = r["month"]
            by_source_map.setdefault(m, {"month": m, "avito": 0, "telegram": 0, "vk": 0, "call": 0, "other": 0, "total": 0})
            key = r["source"] or "other"
            if key not in by_source_map[m]: key = "other"
            by_source_map[m][key] += r["revenue"] or 0
            by_source_map[m]["total"] += r["revenue"] or 0
        by_source = list(by_source_map.values())
        by_source.sort(key=lambda x: x["month"], reverse=True)

        stock = conn.execute("""
            SELECT p.name, SUM(b.qty_remaining) AS qty, SUM(b.qty_remaining * b.purchase_price) AS value 
            FROM batches b JOIN products p ON p.id=b.product_id WHERE b.qty_remaining>0 GROUP BY p.id ORDER BY p.name
        """).fetchall()
        stock = [dict(r) for r in stock]

        # --- НОВЫЙ БЛОК: Сколько чего продано ---
        sold_items_raw = conn.execute("""
            SELECT p.name, SUM(oi.qty) AS qty_sold, SUM(oi.qty * oi.sale_price) AS total_revenue
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'delivered'
            GROUP BY p.id
            ORDER BY qty_sold DESC
        """).fetchall()
        sold_items = [dict(r) for r in sold_items_raw]

        # --- Топ по марже ---
        top_products_raw = conn.execute("""
            SELECT p.name, SUM(oi.qty) AS qty, SUM(oi.qty*oi.sale_price) AS revenue, SUM(oi.cost_total) AS cost 
            FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id 
            WHERE o.status='delivered' GROUP BY p.id ORDER BY (SUM(oi.qty*oi.sale_price) - SUM(oi.cost_total)) DESC LIMIT 15
        """).fetchall()
        top_products = [{
            "name": r["name"], "qty": r["qty"], "revenue": r["revenue"] or 0, "cost": r["cost"] or 0,
            "profit": (r["revenue"] or 0) - (r["cost"] or 0),
        } for r in top_products_raw]

        # --- Графики по дням ---
        min_date_row = conn.execute("""
            SELECT MIN(order_date) AS min_d 
            FROM orders WHERE status='delivered'
        """).fetchone()
        
        if min_date_row and min_date_row["min_d"]:
            start_date_str = min_date_row["min_d"]
        else:
            start_date_str = "2026-11-01"
            
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = date.today()
        
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        date_list = []
        current_date = start_date
        while current_date <= end_date:
            date_list.append(str(current_date))
            current_date += timedelta(days=1)
            
        daily_rev_raw = conn.execute("""
            SELECT order_date AS day, SUM(total) AS revenue 
            FROM orders WHERE status='delivered' AND order_date >= ? GROUP BY day
        """, (start_date_str,)).fetchall()
        
        daily_cost_raw = conn.execute("""
            SELECT o.order_date AS day, SUM(oi.cost_total) AS cost
            FROM orders o JOIN order_items oi ON oi.order_id = o.id 
            WHERE o.status='delivered' AND o.order_date >= ? GROUP BY day
        """, (start_date_str,)).fetchall()
        
        rev_map = {r["day"]: r["revenue"] or 0 for r in daily_rev_raw}
        cost_map_daily = {r["day"]: r["cost"] or 0 for r in daily_cost_raw}
        
        daily_labels = []
        daily_revenue = []
        daily_cost = []
        daily_profit = []
        
        for d in date_list:
            rev = rev_map.get(d, 0)
            cost = cost_map_daily.get(d, 0)
            daily_labels.append(d[5:])
            daily_revenue.append(rev)
            daily_cost.append(cost)
            daily_profit.append(rev - cost)

        raw_source_daily = conn.execute("""
            SELECT order_date AS day, source, SUM(total) AS revenue 
            FROM orders WHERE status='delivered' AND order_date >= ? GROUP BY day, source
        """, (start_date_str,)).fetchall()
        
        source_daily_map = {}
        for r in raw_source_daily:
            d = r["day"]
            if d not in source_daily_map:
                source_daily_map[d] = {"avito": 0, "telegram": 0, "vk": 0, "call": 0, "other": 0}
            key = r["source"] or "other"
            if key not in source_daily_map[d]: key = "other"
            source_daily_map[d][key] += r["revenue"] or 0
            
        source_labels = []
        source_avito = []
        source_telegram = []
        source_vk = []
        source_call = []
        source_other = []
        
        for d in date_list:
            source_labels.append(d[5:])
            day_data = source_daily_map.get(d, {"avito": 0, "telegram": 0, "vk": 0, "call": 0, "other": 0})
            source_avito.append(day_data["avito"])
            source_telegram.append(day_data["telegram"])
            source_vk.append(day_data["vk"])
            source_call.append(day_data["call"])
            source_other.append(day_data["other"])

    return templates.TemplateResponse(request, "reports.html", {
        "monthly": monthly, 
        "by_source": by_source, 
        "stock": stock, 
        "sold_items": sold_items, # <-- Передаем новые данные
        "top_products": top_products,
        
        "daily_labels": json.dumps(daily_labels),
        "daily_revenue": json.dumps(daily_revenue),
        "daily_cost": json.dumps(daily_cost),
        "daily_profit": json.dumps(daily_profit),
        
        "source_labels": json.dumps(source_labels),
        "source_avito": json.dumps(source_avito),
        "source_telegram": json.dumps(source_telegram),
        "source_vk": json.dumps(source_vk),
        "source_call": json.dumps(source_call),
        "source_other": json.dumps(source_other),
        
        "top_names": json.dumps([t["name"] for t in top_products]),
        "top_profits": json.dumps([t["profit"] for t in top_products]),
    })