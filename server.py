# server.py
import asyncio
import itertools
import json
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_RECENT
import openai

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return FileResponse("index.html")

class AvoRequest(BaseModel):
    urls: list[str]
    openai_key: str
    prompt: str
    model: str = "gpt-4o-mini"
    x_bearer_token: str = ""

# ─────────── 공통 헬퍼 ───────────
def clean(text):
    return re.sub(r'\s+', ' ', text).strip()

async def try_selectors(page, selectors):
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            if els:
                return els
        except:
            continue
    return []

# ─────────── YouTube ───────────
def scrape_youtube(url):
    downloader = YoutubeCommentDownloader()
    comments = list(downloader.get_comments_from_url(url, sort_by=SORT_BY_RECENT))
    return [{"platform": "YOUTUBE", "author": c['author'], "date": c['time'], "url": url, "content": c['text'], "type": "댓글"} for c in comments]

# ─────────── DCInside ───────────
async def scrape_dcinside(page, url):
    items = []
    print(f"[DC] 시작: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    print(f"[DC] 페이지 로드 완료")

    title_el = await page.query_selector(".title_subject")
    title = clean(await title_el.inner_text()) if title_el else ""

    # 작성자/날짜 추출
    post_author = await page.evaluate("""() => {
        const nick = document.querySelector('.gall_writer .nickname em');
        const ip = document.querySelector('.gall_writer .ip');
        return (nick ? nick.innerText : '') + (ip ? ip.innerText : '');
    }""")
    post_date = await page.evaluate("""() => {
        const el = document.querySelector('.gallview_head .gall_date, .gall_date');
        return el ? el.innerText.trim() : '최근';
    }""")

    # 노이즈 제거 후 본문 추출
    content = await page.evaluate("""() => {
        const div = document.querySelector('.write_div');
        if (!div) return '';
        const clone = div.cloneNode(true);
        clone.querySelectorAll('.imgwrap, .img_numbering, .og-div, script, .adver_cont, .writing_view_box').forEach(el => el.remove());
        return clone.innerText.trim();
    }""")
    content = clean(content)

    if title or content:
        items.append({"platform": "DCINSIDE", "author": clean(post_author) or "작성자", "date": post_date, "url": url,
                      "content": f"[제목] {title}\n{content}", "type": "게시글"})

    comment_items = await page.query_selector_all(".ub-content")
    print(f"[DC] 댓글 수: {len(comment_items)}")
    for item in comment_items:
        txt_el = await item.query_selector(".cmt_txtbox p.usertxt, .cmt_txtbox p")
        nick_el = await item.query_selector(".nickname em")
        ip_el = await item.query_selector(".nickname .ip")
        date_el = await item.query_selector(".date_time")
        t = clean(await txt_el.inner_text()) if txt_el else ""
        nick = (await nick_el.inner_text() if nick_el else "") + (await ip_el.inner_text() if ip_el else "")
        d = clean(await date_el.inner_text()) if date_el else "최근"
        if len(t) > 2:
            items.append({"platform": "DCINSIDE", "author": clean(nick) or "댓글", "date": d, "url": url, "content": t, "type": "댓글"})
    print(f"[DC] 최종 수집: {len(items)}개")
    return items

# ─────────── 네이트판 ───────────
async def scrape_nate(page, url):
    items = []
    print(f"[NATE] 시작: {url}")
    await page.goto(url, wait_until="load", timeout=60000)
    await page.wait_for_timeout(3000)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)
    print(f"[NATE] 페이지 로드 완료")

    title_el = await page.query_selector("h1")
    content_el = await page.query_selector("div.posting")
    date_el = await page.query_selector(".post-tit-info span.date")
    title = clean(await title_el.inner_text()) if title_el else ""
    content = clean(await content_el.inner_text()) if content_el else ""
    date = clean(await date_el.inner_text()) if date_el else "최근"
    if title or content:
        items.append({"platform": "NATE", "author": "작성자", "date": date, "url": url,
                      "content": f"[제목] {title}\n{content}", "type": "게시글"})

    comment_items = await page.query_selector_all("#commentDiv .cmt_item")
    print(f"[NATE] 댓글 수: {len(comment_items)}")
    for item in comment_items:
        txt_el = await item.query_selector("dd.usertxt span")
        date_i = await item.query_selector("dt i")
        t = clean(await txt_el.inner_text()) if txt_el else ""
        d = clean(await date_i.inner_text()) if date_i else "최근"
        if len(t) > 2:
            items.append({"platform": "NATE", "author": "댓글", "date": d, "url": url, "content": t, "type": "댓글"})
    print(f"[NATE] 최종 수집: {len(items)}개")
    return items

# ─────────── 네이버 뉴스 ───────────
async def scrape_naver(page, url):
    items = []
    print(f"[NAVER] 시작: {url}")
    await page.goto(url, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)
    print(f"[NAVER] 페이지 로드 완료")

    # 기사 본문
    article_el = await page.query_selector("#newsct_article, #articleBodyContents, .go_trans._article_content")
    if article_el:
        t = clean(await article_el.inner_text())
        if t:
            items.append({"platform": "NAVER", "author": "기사", "date": "최근", "url": url, "content": t[:800], "type": "게시글"})

    # 댓글 섹션으로 스크롤
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)

    # "더보기" 버튼 반복 클릭 → 전체 댓글 로드
    page_count = 0
    while True:
        more_btn = await page.query_selector(".u_cbox_btn_more, [class*='cbox_btn_more'], .comment_more_btn")
        if not more_btn:
            break
        try:
            await more_btn.click()
            await page.wait_for_timeout(1500)
            page_count += 1
            print(f"[NAVER] 댓글 더보기 클릭 {page_count}회")
        except:
            break

    # 전체 댓글 수집
    comment_els = await try_selectors(page, [
        ".u_cbox_contents", ".reply_text span", "[class*='cbox_contents']"
    ])
    print(f"[NAVER] 댓글 수: {len(comment_els)}")
    for el in comment_els:
        t = clean(await el.inner_text())
        if len(t) > 2:
            items.append({"platform": "NAVER", "author": "댓글", "date": "최근", "url": url, "content": t, "type": "댓글"})
    print(f"[NAVER] 최종 수집: {len(items)}개")
    return items

# ─────────── 더쿠 ───────────
async def scrape_theqoo(page, url):
    items = []
    print(f"[THEQOO] 시작: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    print(f"[THEQOO] 페이지 로드 완료")

    # 게시글 제목/본문/작성자/날짜
    title_el = await page.query_selector(".theqoo_document_header .title")
    post_content_el = await page.query_selector(".xe_content")
    post_author = await page.evaluate("""() => {
        const sides = document.querySelectorAll('.rd_hd .board .btm_area .side');
        return sides[0] ? sides[0].innerText.trim().split('\\n')[0].trim() : '무명의 더쿠';
    }""")
    post_date = await page.evaluate("""() => {
        const el = document.querySelector('.rd_hd .board .btm_area .side.fr span');
        return el ? el.innerText.trim() : '최근';
    }""")
    title = clean(await title_el.inner_text()) if title_el else ""
    content = clean(await post_content_el.inner_text()) if post_content_el else ""
    if title or content:
        items.append({"platform": "THEQOO", "author": post_author, "date": post_date, "url": url,
                      "content": f"[제목] {title}\n{content}", "type": "게시글"})

    # 댓글
    comment_els = await page.query_selector_all(".fdb_itm")
    print(f"[THEQOO] xe_content/댓글 수: {len(comment_els)}")
    for el in comment_els:
        txt_el = await el.query_selector(".xe_content")
        nick_el = await el.query_selector(".meta span a")
        date_el = await el.query_selector(".meta span.date")
        t = clean(await txt_el.inner_text()) if txt_el else ""
        nick_raw = clean(await nick_el.inner_text()) if nick_el else "무명의 더쿠"
        nick = re.sub(r'^\d+\.\s*', '', nick_raw)  # "1. 무명의 더쿠" → "무명의 더쿠"
        d = clean(await date_el.inner_text()) if date_el else "최근"
        if len(t) > 2:
            items.append({"platform": "THEQOO", "author": nick, "date": d, "url": url, "content": t, "type": "댓글"})
    print(f"[THEQOO] 최종 수집: {len(items)}개")
    return items

# ─────────── X (트위터) ───────────
async def scrape_x(page, url):
    items = []
    print(f"[X] 시작: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)

    # 로그인 요구 팝업 닫기 시도
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except:
        pass

    status_match = re.search(r'/status/(\d+)', url)

    if status_match:
        # 개별 트윗 페이지: 첫 번째가 원본, 나머지는 답글
        els = await page.query_selector_all("[data-testid='tweetText']")
        print(f"[X] 트윗 요소 수: {len(els)}")
        for i, el in enumerate(els):
            t = clean(await el.inner_text())
            if len(t) > 2:
                label = "[트윗]" if i == 0 else "[답글]"
                # 작성자
                try:
                    article = await el.evaluate_handle("el => el.closest('article')")
                    nick_el = await article.query_selector("[data-testid='User-Name'] span")
                    nick = clean(await nick_el.inner_text()) if nick_el else "unknown"
                except:
                    nick = "unknown"
                items.append({"platform": "X", "author": nick, "date": "최근", "url": url, "content": f"{label} {t}", "type": "게시글" if label == "[트윗]" else "댓글"})
    else:
        # 프로필 페이지: 최근 트윗 목록
        await page.evaluate("window.scrollTo(0, 2000)")
        await page.wait_for_timeout(2000)
        els = await page.query_selector_all("article[data-testid='tweet']")
        print(f"[X] 프로필 트윗 수: {len(els)}")
        for el in els:
            try:
                txt_el = await el.query_selector("[data-testid='tweetText']")
                nick_el = await el.query_selector("[data-testid='User-Name'] span")
                t = clean(await txt_el.inner_text()) if txt_el else ""
                nick = clean(await nick_el.inner_text()) if nick_el else "unknown"
                # 트윗 개별 URL 추출
                link_el = await el.query_selector("a[href*='/status/']")
                tw_url = ("https://x.com" + await link_el.get_attribute("href")) if link_el else url
                if len(t) > 2:
                    items.append({"platform": "X", "author": nick, "date": "최근", "url": tw_url, "content": t, "type": "게시글"})
            except:
                continue

    print(f"[X] 최종 수집: {len(items)}개")
    return items

# ─────────── 인스타그램 ───────────
async def scrape_instagram(page, url):
    items = []
    await page.goto(url, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)

    els = await try_selectors(page, [
        "._ap30", "[class*='_a9zs']",
        "ul[class*='comment'] span[dir='auto']",
        "h1", "[role='button'] span[dir='auto']"
    ])
    for el in els:
        t = clean(await el.inner_text())
        if len(t) > 2:
            items.append({"platform": "INSTAGRAM", "author": "게시물/댓글", "date": "최근", "url": url, "content": t})
    return items

# ─────────── 플랫폼 라우터 ───────────
async def scrape_page(context, url):
    page = await context.new_page()
    try:
        u = url.lower()
        if "dcinside.com" in u:
            return await scrape_dcinside(page, url)
        elif "nate.com" in u:
            return await scrape_nate(page, url)
        elif "naver.com" in u:
            return await scrape_naver(page, url)
        elif "theqoo.net" in u:
            return await scrape_theqoo(page, url)
        elif "x.com" in u or "twitter.com" in u:
            return await scrape_x(page, url)
        elif "instagram.com" in u:
            return await scrape_instagram(page, url)
        else:
            return []
    except Exception as e:
        print(f"[ERROR] Scrape error ({url}): {e}")
        return []
    finally:
        await page.close()

# ─────────── 메인 엔드포인트 ───────────
@app.post("/api/process-avo")
async def process_avo(data: AvoRequest):
    all_data = []
    youtube_urls = [u for u in data.urls if "youtube.com" in u or "youtu.be" in u]
    other_urls = [u for u in data.urls if u not in youtube_urls]

    for url in youtube_urls:
        try:
            all_data.extend(scrape_youtube(url))
        except Exception as e:
            print(f"YouTube error: {e}")

    if other_urls:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="ko-KR",
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            tasks = [scrape_page(context, url) for url in other_urls]
            results = await asyncio.gather(*tasks)
            all_data.extend([item for sublist in results for item in sublist])
            await browser.close()

    print(f"[DEBUG] 수집된 전체 데이터 수: {len(all_data)}")
    for d in all_data[:5]:
        print(f"[DEBUG] {d['platform']} | {d['content'][:80]}")

    if not all_data:
        raise HTTPException(status_code=404, detail="데이터를 수집하지 못했습니다.")

    client = openai.OpenAI(api_key=data.openai_key)
    gpt_input = [{"id": i, "type": d.get("type", ""), "text": d['content'][:1000]} for i, d in enumerate(all_data)]

    system_prompt = (
        "너는 온라인 커뮤니티 텍스트 분석 전문가야.\n"
        "각 항목에는 'type' 필드가 있으며 '게시글' 또는 '댓글'로 구분돼.\n"
        "제시된 텍스트 목록에서 아래 [선별 기준]에 해당하는 항목의 ID 번호만 골라내어 리스트 형식으로 응답해.\n"
        "게시글과 댓글 모두 기준에 맞으면 선별 대상이야.\n"
        "응답은 반드시 [0, 1, 2] 처럼 숫자 리스트만 출력해. 해당 항목이 없으면 []를 출력해."
    )

    try:
        response = client.chat.completions.create(
            model=data.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[선별 기준]: {data.prompt}\n\n[데이터 목록]:\n{json.dumps(gpt_input, ensure_ascii=False)}"}
            ],
            temperature=0
        )
        ai_res = response.choices[0].message.content
        selected_ids = [int(n) for n in re.findall(r'\d+', ai_res)]
        final_results = [all_data[i] for i in selected_ids if i < len(all_data)]
        return {"success": True, "results": final_results, "prompt": data.prompt}
    except Exception as e:
        print(f"GPT Error: {e}")
        return {"success": False, "message": "AI 분석 중 오류가 발생했습니다."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
