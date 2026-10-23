
import asyncio
import ipaddress
import socket
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import sys
from PIL import Image, UnidentifiedImageError
from playwright.async_api import async_playwright
import traceback
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"
from urllib.parse import urldefrag, urlparse
MAX_PAGES = 150
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

async def capture_comic_images(page, output_dir: Path) -> list[Path]:
    import asyncio
    from io import BytesIO

    import pymupdf
    from PIL import Image, UnidentifiedImageError

    output_dir.mkdir(parents=True, exist_ok=True)

    captured = {}
    pending = set()

    async def save_response(response):
        try:
            if response.status != 200:
                return

            content_type = (
                response.headers.get("content-type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if not content_type.startswith("image/"):
                return

            body = await response.body()

            if not body or len(body) > MAX_IMAGE_BYTES:
                return

            try:
                with Image.open(BytesIO(body)) as image:
                    image.load()

                    if image.width < 200 or image.height < 200:
                        return

                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        return

                    image.convert("RGB").save(
                        output_dir / f"page_{len(captured) + 1:03d}.jpg",
                        "JPEG",
                        quality=95,
                    )

                    captured[response.url] = (
                        output_dir / f"page_{len(captured):03d}.jpg"
                    )

            except (UnidentifiedImageError, OSError):
                return

        except Exception as exc:
            print("Image response capture error:", exc)

    def on_response(response):
        task = asyncio.create_task(save_response(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    page.on("response", on_response)

    previous_height = 0
    unchanged_rounds = 0

    for _ in range(40):
        await page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"
        )
        await page.wait_for_timeout(800)

        height = await page.evaluate(
            "() => document.body.scrollHeight"
        )

        if height == previous_height:
            unchanged_rounds += 1
        else:
            unchanged_rounds = 0

        previous_height = height

        if unchanged_rounds >= 3:
            break

    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)

    page.remove_listener("response", on_response)

    return list(output_dir.glob("page_*.jpg"))


def check_public_url(url: str) -> None:
    """Reject non-HTTPS URLs and non-public destinations."""
    parsed = urlsplit(url)

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
      ):
        raise ValueError("Only public HTTPS URLs are accepted.")

    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443,
                type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError) as exc:
        raise ValueError("Could not validate URL host.") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private or non-public network destinations are blocked.")



async def import_reader_url(url: str) -> dict:
    import asyncio
    import shutil
    from io import BytesIO
    from uuid import uuid4

    check_public_url(url)

    job_id = str(uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    saved = []
    captured = {}
    pending = set()
    browser = None
    context = None
    page = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            context = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                viewport={"width": 1440, "height": 1000},
            )

            async def guard_route(route):
                try:
                    check_public_url(route.request.url)
                    await route.continue_()
                except Exception:
                    await route.abort()

            await context.route("**/*", guard_route)
            page = await context.new_page()

            # Capture image responses.
            async def capture_response(response):
                try:
                    if response.status != 200:
                        return

                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )

                    if not content_type.startswith("image/"):
                        return

                    body = await response.body()

                    if not body or len(body) > MAX_IMAGE_BYTES:
                        return

                    captured[response.url] = body
                    captured[response.request.url] = body

                except Exception as exc:
                    print(
                        "Image capture error:",
                        type(exc).__name__,
                        str(exc),
                    )

            def on_response(response):
                task = asyncio.create_task(capture_response(response))
                pending.add(task)
                task.add_done_callback(pending.discard)

            page.on("response", on_response)

            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                if response is None:
                    raise ValueError("No main-document response received.")

                print("READER HTTP STATUS:", response.status)
                print("FINAL READER URL:", page.url)

                if response.status >= 400:
                    raise ValueError(
                        f"Reader returned HTTP {response.status}. "
                        "The website may deny automated access."
                    )

                await page.wait_for_timeout(4000)

                # Slowly scroll through the reader to trigger lazy loading.
                previous_height = 0
                previous_count = 0
                stable_rounds = 0
                no_progress_rounds = 0

                for step in range(100):
                    await page.evaluate("""
                        () => window.scrollBy({
                            top: Math.max(350, window.innerHeight * 0.55),
                            left: 0,
                            behavior: "smooth"
                        })
                    """)

                    await page.wait_for_timeout(1200)

                    # Allow visible images to finish loading.
                    await page.evaluate("""
                        async () => {
                            const imgs = Array.from(document.images);
                            await Promise.all(imgs.map(img => {
                                if (img.complete) return Promise.resolve();
                                return new Promise(resolve => {
                                    img.addEventListener("load", resolve, {once:true});
                                    img.addEventListener("error", resolve, {once:true});
                                    setTimeout(resolve, 2500);
                                });
                            }));
                        }
                    """)

                    height = await page.evaluate(
                        "() => document.documentElement.scrollHeight"
                    )
                    count = await page.locator("img").count()

                    stable_rounds = (
                        stable_rounds + 1 if height == previous_height else 0
                    )
                    no_progress_rounds = (
                        no_progress_rounds + 1
                        if count == previous_count
                        else 0
                    )

                    previous_height = height
                    previous_count = count

                    print(
                        f"Scroll {step + 1}: "
                        f"height={height}, img_elements={count}"
                    )

                    if stable_rounds >= 8 and no_progress_rounds >= 8:
                        break

                await page.wait_for_timeout(2000)

                # Inspect all image elements in DOM order.
                images = await page.locator("img").evaluate_all("""
         elements => elements.map((img, index) => ({
        index: index + 1,
        src: img.currentSrc || img.src || "",
        dataSrc: img.getAttribute("data-src") || "",
        dataLazySrc: img.getAttribute("data-lazy-src") || "",
        srcset: img.getAttribute("srcset") || "",
        width: img.naturalWidth,
        height: img.naturalHeight,
        complete: img.complete,
        className: String(img.className || ""),
        alt: img.alt || ""
    }))

                  
                """)

                print("TOTAL IMAGE ELEMENTS:", len(images))

                # Keep unique, loaded, sufficiently large HTTP(S) images.
                unique = []
                seen = set()

                for item in images:
                    src = item.get("src", "")
                    #try lazy-loading atributes if main source is missing
                    if not src or src.startswith("data:"):
                        src = item.get("dataSrc") or item.get("dataLazySrc") or ""

                    if not src or src in seen:
                        continue

                    if not src.startswith(("http://", "https://")):
                        continue

                    if (
                        not item.get("complete")
                        or item.get("width", 0) < 200
                        or item.get("height", 0) < 200
                    ):
                         if not item.get("dataSrc") and not item.get("dataLazySrc"):
                          continue

                    seen.add(src)
                    unique.append(item)

                print("UNIQUE LOADED IMAGE CANDIDATES:", len(unique))

                # Wait for response handlers to finish.
                while pending:
                    await asyncio.gather(
                        *list(pending),
                        return_exceptions=True,
                    )

                print("IMAGE RESPONSES CAPTURED:", len(captured))

                if not unique:
                    raise ValueError(
                        "No loaded comic images found. "
                        "The reader may use a different rendering method."
                    )

                if len(unique) > MAX_PAGES:
                    raise ValueError(
                        f"Found {len(unique)} image candidates; "
                        f"the configured limit is {MAX_PAGES}."
                    )

                # Match URLs even if the fragment differs.
                def find_body(image_url):
                    body = captured.get(image_url)
                    if body is not None:
                        return body

                    from urllib.parse import urldefrag
                    target, _ = urldefrag(image_url)

                    for captured_url, data in captured.items():
                        candidate, _ = urldefrag(captured_url)
                        if candidate == target:
                            return data

                    return None

                # Save pages in DOM order.
                for item in unique:
                    image_url = item["src"]

                    if not image_url.startswith(("http://","https://")):
                        continue

                    parsed=urlparse(image_url)

                    if parsed.scheme !="https" or not parsed.hostname:
                     print("skipping unsupported image url ",image_url[:120])
                     continue

                    body = find_body(image_url)

                    if body is None:
                         try:
                            response = await page.context.request.get(
                                image_url,
                                timeout=20000,
                                headers={"Referer": page.url},
                            )

                            if response.ok:
                                body = await response.body()
                            else:
                                print(
                                    "Image fetch failed:",
                                    response.status,
                                    image_url[:150],
                                )
                            
                         except Exception as exc:
                          print(
                                "Image fetch error:",
                                image_url[:120],
                                str(exc),
                            )
                        

                    try:
                        with Image.open(BytesIO(body)) as image:
                            image.load()

                            if image.width * image.height > MAX_IMAGE_PIXELS:
                                print("Skipping oversized image:", image_url[:120])
                                continue

                              # Ignore tiny tracking images and placeholders.
                            if image.width < 200 or image.height < 200:
                                print(
                                    "Skipping small image:",
                                    image.width,
                                    image.height,
                                )
                                continue
                              
                            output = job_dir / f"page_{len(saved) + 1:03d}.png"

                            image.convert("RGB").save(output, format="PNG")
                            saved.append(output.name)

                    except (UnidentifiedImageError, OSError) as exc:
                        print(
                            "Invalid image response:",
                            image_url[:150],
                            str(exc),
                        )

                print("SAVED COMIC PAGES:", len(saved))

                if not saved:
                    raise ValueError(
                        "No comic pages were saved. See backend image logs."
                    )

            finally:
                page.remove_listener("response", on_response)

                if context is not None:
                    await context.close()

                if browser is not None:
                    await browser.close()

        return {
            "job_id": job_id,
            "page_count": len(saved),
            "pages": [f"/files/{job_id}/{name}" for name in saved],
            "source_type": "reader_url",
        }

    except Exception:
        traceback.print_exc()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


async def import_comic_reader(url: str) -> dict:
    def run_playwright():
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()

        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(import_reader_url(url))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    return await asyncio.to_thread(run_playwright)




async def import_downloadable_comic(url: str) -> dict:
    import asyncio
    import sys
    from pathlib import Path
    from uuid import uuid4
    from io import BytesIO
    from urllib.parse import urlparse

    from playwright.async_api import async_playwright
    from PIL import Image, UnidentifiedImageError

    MAX_PAGES = 200
    MAX_IMAGE_PIXELS = 40_000_000

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Please provide a valid HTTPS comic URL.")

    async def download():
        base_dir = Path(__file__).resolve().parent
        job_id = uuid4().hex
        pages_dir = base_dir / "data" / "jobs" / job_id / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        saved = []
        browser = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                context = await browser.new_context(
                    accept_downloads=True
                )
                page = await context.new_page()

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                if response is None or response.status >= 400:
                    status = response.status if response else "no response"
                    raise ValueError(
                        f"Could not open comic page. HTTP status: {status}"
                    )

                await page.wait_for_timeout(1500)

                # Scroll through the reader to trigger lazy-loaded pages.
                previous_height = 0
                stable_rounds = 0

                for _ in range(250):
                    height = await page.evaluate(
                        "() => document.documentElement.scrollHeight"
                    )

                    await page.evaluate(
                        "() => window.scrollBy(0, Math.max(500, window.innerHeight * 0.8))"
                    )
                    await page.wait_for_timeout(300)

                    new_height = await page.evaluate(
                        "() => document.documentElement.scrollHeight"
                    )

                    if new_height == previous_height == height:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0

                    previous_height = new_height

                    at_bottom = await page.evaluate(
                        "() => window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5"
                    )

                    if at_bottom and stable_rounds >= 2:
                        break

                # Return actual image URLs and common lazy-load attributes.
                images = await page.locator("img").evaluate_all("""
                    elements => elements.map(img => ({
                        urls: [
                            img.currentSrc,
                            img.getAttribute("data-src"),
                            img.getAttribute("data-lazy-src"),
                            img.getAttribute("data-original"),
                            img.getAttribute("data-full"),
                            img.getAttribute("src")
                        ].filter(Boolean),
                        width: img.naturalWidth || 0,
                        height: img.naturalHeight || 0,
                        alt: img.alt || ""
                    }))
                """)

                targets = []
                seen = set()

                for item in images:
                    # Prefer the first real URL, not an SVG placeholder.
                    image_url = next(
                        (
                            candidate
                            for candidate in item["urls"]
                            if candidate.startswith("https://")
                            and not candidate.startswith("data:")
                        ),
                        None,
                    )

                    if not image_url or image_url in seen:
                        continue

                    # Exclude clearly small, already-loaded images.
                    if (
                        item["width"] > 0
                        and item["height"] > 0
                        and (
                            item["width"] < 200
                            or item["height"] < 200
                        )
                    ):
                        continue

                    seen.add(image_url)
                    targets.append(image_url)

                print("IMAGE ELEMENTS:", len(images))
                print("IMAGE URL CANDIDATES:", len(targets))

                if not targets:
                    raise ValueError(
                        "No usable image URLs found. "
                        "This page may use a different download mechanism."
                    )

                if len(targets) > MAX_PAGES:
                    print(
                        f"Limiting {len(targets)} image candidates "
                        f"to {MAX_PAGES}."
                    )
                    targets = targets[:MAX_PAGES]

                # Fetch each image in DOM order using the browser context.
                for image_url in targets:
                    try:
                        result = await context.request.get(
                            image_url,
                            timeout=30000,
                            headers={"Referer": page.url},
                        )

                        if not result.ok:
                            print(
                                "IMAGE REQUEST FAILED:",
                                result.status,
                                image_url[:150],
                            )
                            continue

                        content_type = result.headers.get(
                            "content-type", ""
                        ).lower()

                        body = await result.body()

                        if not body:
                            print("EMPTY IMAGE RESPONSE:", image_url[:120])
                            continue

                        # Validate content instead of trusting the URL suffix.
                        with Image.open(BytesIO(body)) as image:
                            image.load()

                            if image.width * image.height > MAX_IMAGE_PIXELS:
                                print("Skipping oversized image:", image_url[:120])
                                continue

                            if image.width < 200 or image.height < 200:
                                print("Skipping small image:", image_url[:120])
                                continue

                            output = pages_dir / (
                                f"page-{len(saved) + 1:03d}.png"
                            )
                            image.convert("RGB").save(
                                output,
                                format="PNG",
                            )
                            saved.append(output.name)

                            print(
                                f"SAVED {output.name}: "
                                f"{image.width}x{image.height}"
                            )

                    except (
                        UnidentifiedImageError,
                        OSError,
                        ValueError,
                    ) as exc:
                        print(
                            "SKIPPING NON-IMAGE:",
                            image_url[:120],
                            str(exc),
                        )
                    except Exception as exc:
                        print(
                            "IMAGE DOWNLOAD ERROR:",
                            image_url[:120],
                            str(exc),
                        )

                if not saved:
                    raise ValueError(
                        "No comic pages were saved. Check the image request "
                        "statuses above. The site may require a separate "
                        "download button or a different image URL."
                    )

                return {
                    "job_id": job_id,
                    "page_count": len(saved),
                    "pages": [
                        f"/files/{job_id}/pages/{name}"
                        for name in saved
                    ],
                    "next_step": "comic_text_extraction",
                }

        finally:
            if browser is not None:
                await browser.close()

    def run():
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()

        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(download())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    return await asyncio.to_thread(run)
