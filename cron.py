import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlparse

import uuid
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# =========================
# Configuration variables
# =========================
CATALOG_URL = os.getenv("DOMAIN")  # <- set your catalog URL
OUTPUT = "products.json"  # <- output JSON filename

IMAGES_DIR = "product_images"  # <- base folder for saving product images
CONCURRENCY = 8  # <- max concurrent product page requests
IMAGE_CONCURRENCY = 10  # <- max concurrent image downloads
USER_AGENT = (
    "Mozilla/5.0 (compatible; CatalogScraperAsync/1.3; +https://example.com/bot)"
)
TIMEOUT = 30  # <- total timeout per request (seconds)

DOWNLOAD_SKIP_EXISTS = True  # <- skip downloads if file already exists
CLEAN_BEFORE_RUN = True  # <- remove OUTPUT and IMAGES_DIR at startup


def canonical_product_url(product_url: str) -> str:
    """
    Normalize the product URL for ID generation:
    - Keep scheme, host (lowercased), and path
    - Drop query and fragment
    - Strip trailing slash from path
    """
    parsed = urlparse(product_url or "")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def product_unique_id(product_url: str) -> str:
    """
    Deterministic unique ID based on the canonical product URL using UUID v5.
    """
    canonical = canonical_product_url(product_url)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, canonical))


def slugify(text, fallback="product"):
    """
    Make a filesystem-safe slug from a title. If empty, use fallback.
    """
    if not text:
        text = fallback
    text = " ".join(str(text).strip().split()).lower()
    text = re.sub(r"[^\w\- ]+", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    return text or fallback


def unique_folder(base_dir: Path, name: str) -> Path:
    """
    Return a unique folder path under base_dir for 'name' (append -1, -2, ... if needed).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    candidate = base_dir / name
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        candidate = base_dir / f"{name}-{idx}"
        if not candidate.exists():
            return candidate
        idx += 1


def clean_outputs(output_file: str, images_dir: str):
    """
    Remove the output JSON file and the entire images directory (recursively) if they exist.
    """
    try:
        if os.path.isfile(output_file):
            os.remove(output_file)
            print(f"Removed file: {output_file}")
    except Exception as e:
        print(f"WARN: Failed to remove file '{output_file}': {e}")

    try:
        if os.path.isdir(images_dir):
            shutil.rmtree(images_dir)
            print(f"Removed folder: {images_dir}")
    except Exception as e:
        print(f"WARN: Failed to remove folder '{images_dir}': {e}")


async def get_soup(url, session):
    async with session.get(url) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return BeautifulSoup(html, "html.parser")


async def extract_product_links(catalog_url, session):
    soup = await get_soup(catalog_url, session)
    links = set()

    # Find product links within each product element container
    for div in soup.select("div.product-element-bottom"):
        for a in div.select("a[href]"):
            href = a.get("href")
            if not href:
                continue
            full_url = urljoin(catalog_url, href)
            if urlparse(full_url).scheme in ("http", "https"):
                links.add(full_url)

    return sorted(links)


def parse_product_details(soup, product_url):
    title_el = soup.select_one("h1.product_title")
    price_el = soup.select_one("p.price bdi")  # first bdi under p.price
    desc_el = soup.select_one("div#tab-description")

    # Images: find anchor hrefs inside figure.woocommerce-product-gallery__image
    image_urls = []
    for fig in soup.select("figure.woocommerce-product-gallery__image"):
        a = fig.select_one("a[href]")
        if a:
            href = a.get("href")
            if href:
                image_urls.append(urljoin(product_url, href))

    # Deduplicate while preserving order
    image_urls = list(dict.fromkeys(image_urls))

    title = " ".join(title_el.get_text(strip=True).split()) if title_el else None
    price = price_el.get_text(strip=True) if price_el else None
    description = desc_el.get_text(" ", strip=True) if desc_el else None

    # Unique, stable ID from canonical URL
    uid = product_unique_id(product_url)

    return {
        "id": uid,  # <- added
        "title": title,
        "price": price,
        "description": description,
        "images": image_urls,
        "url": product_url,
    }


async def fetch_product_details(product_url, session, sem):
    async with sem:
        try:
            soup = await get_soup(product_url, session)
            details = parse_product_details(soup, product_url)
            if details.get("title"):
                print(
                    f"OK: {details.get('title')} | Price: {details.get('price')} | Images: {len(details.get('images', []))}"
                )
            else:
                print(f"WARN: No title found -> {product_url}")
            return details
        except aiohttp.ClientResponseError as e:
            print(f"HTTP error {e.status} -> {product_url}")
        except aiohttp.ClientError as e:
            print(f"Request error {e} -> {product_url}")
        except Exception as e:
            print(f"Unexpected error {e} -> {product_url}")
        return None


def sanitize_filename_from_url(img_url: str, index: int):
    """
    Build a safe filename using the URL's basename; fallback to index with .jpg.
    """
    path = urlparse(img_url).path
    base = os.path.basename(path) or f"image-{index}.jpg"
    base = re.sub(r"[^\w\.\-]+", "-", base)
    return base


async def download_image(img_url, dest_path: Path, session, img_sem):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if DOWNLOAD_SKIP_EXISTS and dest_path.exists():
        return dest_path

    async with img_sem:
        try:
            async with session.get(img_url) as resp:
                resp.raise_for_status()
                content = await resp.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            return dest_path
        except aiohttp.ClientResponseError as e:
            print(f"IMG HTTP {e.status} -> {img_url}")
        except aiohttp.ClientError as e:
            print(f"IMG Request error {e} -> {img_url}")
        except Exception as e:
            print(f"IMG Unexpected error {e} -> {img_url}")
        return None


async def download_images_for_product(product: dict, session, img_sem, base_dir: Path):
    """
    Create a folder per product and download all its images. Updates product dict
    with 'image_files' list of saved file paths (relative to base_dir).
    """
    title = product.get("title")
    url = product.get("url") or ""
    fallback_name = slugify(
        os.path.splitext(os.path.basename(urlparse(url).path))[0] or "product"
    )
    folder_name = slugify(title, fallback=fallback_name)
    product_dir = unique_folder(base_dir, folder_name)

    image_urls = product.get("images", [])
    tasks = []
    for i, img_url in enumerate(image_urls, start=1):
        fname = sanitize_filename_from_url(img_url, i)
        dest = product_dir / fname
        tasks.append(download_image(img_url, dest, session, img_sem))

    saved_paths = await asyncio.gather(*tasks)
    rel_paths = [
        str(Path(os.path.relpath(p, base_dir))) if p else None for p in saved_paths
    ]
    rel_paths = [p for p in rel_paths if p]
    product["image_files"] = rel_paths
    product["images_folder"] = str(product_dir.relative_to(base_dir))
    return product


async def scrape_catalog():
    # Clean outputs before running
    if CLEAN_BEFORE_RUN:
        clean_outputs(OUTPUT, IMAGES_DIR)

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    headers = {"User-Agent": USER_AGENT}

    base_images_dir = Path(IMAGES_DIR)
    base_images_dir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        print("Loading catalog...")
        product_links = await extract_product_links(CATALOG_URL, session)
        print(f"Found {len(product_links)} candidate links")

        sem = asyncio.Semaphore(CONCURRENCY)
        detail_tasks = [
            fetch_product_details(url, session, sem) for url in product_links
        ]
        results = await asyncio.gather(*detail_tasks)

        products = [r for r in results if isinstance(r, dict) and r.get("title")]

        # Download images per product with separate concurrency limit
        img_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
        print(f"Downloading images to '{base_images_dir}'...")
        dl_tasks = [
            download_images_for_product(prod, session, img_sem, base_images_dir)
            for prod in products
        ]
        products_with_images = await asyncio.gather(*dl_tasks)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(products_with_images, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products_with_images)} products to {OUTPUT}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(scrape_catalog())
