import asyncio
import logging
import os
import secrets

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright

app = FastAPI(title="소상공인24 수집 테스트")
logger = logging.getLogger("uvicorn.error")

SOURCE_URL = "https://www.sbiz24.kr/#/combinePbancList"
collection_lock = asyncio.Lock()

# 이 서버는 소상공인24만 수집합니다.
# 요청으로 임의의 사이트 주소를 받을 수 없도록 고정했습니다.
EXTRACT_JS = r"""
() => {
    const clean = value =>
        String(value || "").replace(/\s+/g, " ").trim();

    const result = [];
    const seen = new Set();

    for (const a of document.querySelectorAll("a[href]")) {
        if (!a.getClientRects().length) continue;

        const href = a.getAttribute("href") || "";
        let url;

        try {
            url = new URL(href, location.href);
        } catch (_) {
            continue;
        }

        if (url.hostname !== "www.sbiz24.kr") continue;

        // #/extldPbanc/... 등 공고 상세 링크를 찾습니다.
        // 공고 목록 메뉴 자체는 제외합니다.
        if (!/^#\/[^/?#]*pbanc[^/?#]*[/?]/i.test(url.hash)) {
            continue;
        }

        if (/^#\/combinePbancList/i.test(url.hash)) continue;

        const title = clean(
            a.getAttribute("title") || a.innerText || a.textContent
        ).replace(/\s*상세보기\s*$/, "").trim();

        if (title.length < 4 || seen.has(url.href)) continue;

        seen.add(url.href);

        result.push({
            title,
            url: url.href,
            original_href: href
        });
    }

    return result;
}
"""

WAIT_JS = r"""
() => Array.from(document.querySelectorAll("a[href]")).some(a => {
    if (!a.getClientRects().length) return false;

    try {
        const url = new URL(a.getAttribute("href"), location.href);

        return url.hostname === "www.sbiz24.kr"
            && /^#\/[^/?#]*pbanc[^/?#]*[/?]/i.test(url.hash)
            && !/^#\/combinePbancList/i.test(url.hash);
    } catch (_) {
        return false;
    }
})
"""


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "sbiz24-collector",
        "api_key_configured": bool(os.getenv("API_KEY")),
    }


async def collect_first_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )

        try:
            context = await browser.new_context(
                locale="ko-KR",
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()

            response = await page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            if response and response.status >= 400:
                raise RuntimeError(
                    f"목록 페이지 HTTP 오류: {response.status}"
                )

            # 고정된 시간만 기다리지 않고,
            # 실제 공고 상세 링크가 표시되는지 확인합니다.
            await page.wait_for_function(
                WAIT_JS,
                timeout=30000,
            )

            posts = await page.evaluate(EXTRACT_JS)

            if not posts:
                raise RuntimeError(
                    "공고를 추출하지 못했습니다. "
                    "페이지 로딩 또는 링크 구조를 확인해야 합니다."
                )

            return {
                "status": "ok",
                "source": "소상공인24",
                "page_url": page.url,
                "page_title": await page.title(),
                "scope": "현재 목록 첫 페이지",
                "count": len(posts),
                "posts": posts,
            }

        finally:
            await browser.close()


@app.post("/scrape")
async def scrape(
    x_api_key: str | None = Header(default=None),
):
    expected_key = os.getenv("API_KEY", "")

    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Railway Variables에 API_KEY를 설정하세요.",
        )

    if not x_api_key or not secrets.compare_digest(
        x_api_key, expected_key
    ):
        raise HTTPException(
            status_code=401,
            detail="API 키가 없거나 일치하지 않습니다.",
        )

    if collection_lock.locked():
        raise HTTPException(
            status_code=429,
            detail="현재 수집 중입니다. 잠시 후 다시 요청하세요.",
        )

    async with collection_lock:
        try:
            return await asyncio.wait_for(
                collect_first_page(),
                timeout=75,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="수집 제한시간을 초과했습니다.",
            )
        except Exception:
            logger.exception("소상공인24 수집 실패")
            raise HTTPException(
                status_code=502,
                detail=(
                    "수집 실패. Railway Logs에서 원인을 확인하세요. "
                    "공고 0건으로 처리하지 않았습니다."
                ),
            )
