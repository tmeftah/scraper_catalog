from pathlib import Path
from typing import List, Dict, Any, Optional
import json
import re
from urllib.parse import quote_plus, unquote_plus

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Configuration
PRODUCTS_JSON_PATH = Path("products.json")
IMAGES_DIR = Path("product_images")
MARGIN_CHOICES = [30, 40, 50, 60, 70, 80, 90, 100]
PER_PAGE_DEFAULT = "20"
PER_PAGE_CHOICES = ["20", "50", "100", "all"]

# FastAPI app
app = FastAPI()

# Serve product images (always mount, even if dir doesn't exist yet)
app.mount(
    "/images", StaticFiles(directory=str(IMAGES_DIR), check_dir=False), name="images"
)

# Templates
templates = Jinja2Templates(directory="templates")

# In-memory products
PRODUCTS: List[Dict[str, Any]] = []


def load_products() -> List[Dict[str, Any]]:
    if not PRODUCTS_JSON_PATH.exists():
        return []
    with PRODUCTS_JSON_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    result = []
    for p in data:
        p = {
            "title": p.get("title"),
            "price": p.get("price"),
            "description": p.get("description"),
            "images": p.get("images", []),
            "image_files": p.get("image_files", []),
            "url": p.get("url"),
        }
        parse_price_info(p)
        result.append(p)
    return result


CURRENCY_TOKENS = [
    "DT",
    "S/.",
    "zł",
    "CHF",
    "AED",
    "SAR",
    "Kč",
    "lei",
    "kr",
    "€",
    "$",
    "£",
    "₪",
    "₽",
    "₺",
    "₹",
    "¥",
    "₩",
    "₫",
    "₴",
    "₦",
    "฿",
    "₱",
    "₲",
    "₡",
]


def parse_price_info(product: Dict[str, Any]) -> None:
    s = str(product.get("price") or "")
    first_digits = re.search(r"\d", s)
    token = ""
    token_idx = -1
    for t in CURRENCY_TOKENS:
        idx = s.find(t)
        if idx != -1 and (token_idx == -1 or idx < token_idx):
            token, token_idx = t, idx
    position = "prefix"
    if token and first_digits:
        position = "prefix" if token_idx <= first_digits.start() else "suffix"

    compact = re.sub(r"\s+", "", s)
    num_match = re.search(r"[0-9\.,]+", compact)
    num_str = num_match.group(0) if num_match else ""
    if "," in num_str and "." in num_str:
        num_str = num_str.replace(",", "")
    elif "," in num_str and "." not in num_str:
        num_str = num_str.replace(",", ".")
    parts = num_str.split(".")
    if len(parts) > 2:
        dec = parts.pop()
        num_str = "".join(parts) + "." + dec
    try:
        value = float(num_str)
    except Exception:
        value = float("nan")

    product["_priceValue"] = value
    product["_currencySymbol"] = token
    product["_currencyPosition"] = position


def format_with_currency(value: float, product: Dict[str, Any]) -> str:
    if value is None or not (value == value):  # NaN check
        return str(product.get("price") or "")
    num_str = f"{value:,.2f}"
    num_str = re.sub(r"(?<=\d),(?=\d{3}\b)", ",", num_str)
    sym = product.get("_currencySymbol") or ""
    position = product.get("_currencyPosition") or "prefix"
    return (
        f"{sym} {num_str}"
        if sym and position == "prefix"
        else (f"{num_str} {sym}" if sym else num_str)
    )


def compute_final_price_text(product: Dict[str, Any], margin: int) -> str:
    base_val = product.get("_priceValue")
    final_val = base_val * (1 + margin / 100) if base_val == base_val else float("nan")
    return format_with_currency(final_val, product)


def product_thumbnail_url(product: Dict[str, Any]) -> str:
    files = product.get("image_files") or []
    if files:
        return "/images/" + files[0].replace("\\", "/")
    imgs = product.get("images") or []
    if imgs:
        return imgs[0]
    return "https://via.placeholder.com/600x400?text=No+Image"


def product_image_urls(product: Dict[str, Any]) -> List[str]:
    files = product.get("image_files") or []
    if files:
        return ["/images/" + f.replace("\\", "/") for f in files]
    imgs = product.get("images") or []
    return imgs


def find_product_by_title(title: str) -> Optional[Dict[str, Any]]:
    for p in PRODUCTS:
        if (p.get("title") or "") == title:
            return p
    return None


def resolve_page_size(per_page: str, total_items: int) -> int:
    if str(per_page).lower() in ("all", "*"):
        # show everything on one page
        return max(total_items, 0)
    try:
        n = int(per_page)
        if n <= 0:
            return int(PER_PAGE_DEFAULT)
        return n
    except Exception:
        return int(PER_PAGE_DEFAULT)


@app.on_event("startup")
def startup_event():
    global PRODUCTS
    PRODUCTS = load_products()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    margin: int = 30,
    page: int = 1,
    per_page: str = PER_PAGE_DEFAULT,
):
    if margin not in MARGIN_CHOICES:
        margin = 30
    if page < 1:
        page = 1

    q_norm = q.strip().lower()
    global PRODUCTS
    PRODUCTS = load_products()
    filtered = [p for p in PRODUCTS if q_norm in (p.get("title") or "").lower()]

    total = len(filtered)
    page_size = resolve_page_size(per_page, total)

    if total == 0:
        pages = 1
        page = 1
        paged = []
    else:
        if page_size == 0 or page_size >= total:
            pages = 1
            page = 1
            paged = filtered
        else:
            pages = max(1, (total + page_size - 1) // page_size)
            if page > pages:
                page = pages
            start = (page - 1) * page_size
            end = start + page_size
            paged = filtered[start:end]

    items = []
    for p in paged:
        final_txt = compute_final_price_text(p, margin)
        items.append(
            {
                "title": p.get("title"),
                "thumbnail": product_thumbnail_url(p),
                "final_price": final_txt,
                "detail_href": f"/product?title={quote_plus(p.get('title') or '')}&margin={margin}&q={quote_plus(q)}&per_page={quote_plus(per_page)}",
            }
        )

    pagination = {
        "page": page,
        "pages": pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": page - 1 if page > 1 else 1,
        "next_page": page + 1 if page < pages else pages,
        "numbers": list(range(1, pages + 1)),
        "per_page": per_page,
        "page_size": page_size if page_size else total,
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "items": items,
            "q": q,
            "margin": margin,
            "margin_choices": MARGIN_CHOICES,
            "per_page": per_page,
            "per_page_choices": PER_PAGE_CHOICES,
            "count": total,
            "pagination": pagination,
        },
    )


@app.get("/product", response_class=HTMLResponse)
async def product_detail(
    request: Request,
    title: str,
    margin: int = 30,
    q: str = "",
    per_page: str = PER_PAGE_DEFAULT,
):
    # Reload products.json on every product detail request
    global PRODUCTS
    PRODUCTS = load_products()

    if margin not in MARGIN_CHOICES:
        margin = 30
    title = unquote_plus(title)
    p = find_product_by_title(title)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    final_txt = compute_final_price_text(p, margin)
    images = product_image_urls(p)
    # Back link keeps current search query, margin, and per_page
    back_params = f"margin={margin}&per_page={quote_plus(per_page)}"
    if q:
        back_params = f"q={quote_plus(q)}&" + back_params
    back_href = f"/?{back_params}"

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "product": {
                "title": p.get("title"),
                "description": p.get("description"),
                "images": images,
                "final_price": final_txt,
            },
            "q": q,
            "margin": margin,
            "margin_choices": MARGIN_CHOICES,
            "per_page": per_page,
            "per_page_choices": PER_PAGE_CHOICES,
            "back_href": back_href,
        },
    )
