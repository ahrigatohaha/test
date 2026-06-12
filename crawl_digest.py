from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KST = ZoneInfo("Asia/Seoul")
LIST_URL = "https://gall.dcinside.com/mgallery/board/lists/"
VIEW_URL = "https://gall.dcinside.com/mgallery/board/view/"
COMMENT_URL = "https://gall.dcinside.com/board/comment/"
GALLERY_ID = "thesingularity"
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)


def clean(value: str) -> str:
    value = html.unescape(value or "")
    value = URL_RE.sub("", value)
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def soup_text(value: str) -> str:
    return clean(BeautifulSoup(value or "", "lxml").get_text("\n", strip=True))


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=6, connect=6, read=6, status=6, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=6))
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132 Safari/537.36", "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7"})
    return s


S = session()
LAST = 0.0


def throttle(delay: float = 0.15):
    global LAST
    wait = delay - (time.monotonic() - LAST)
    if wait > 0:
        time.sleep(wait)
    LAST = time.monotonic()


def get(url, **kwargs):
    throttle()
    r = S.get(url, timeout=35, **kwargs)
    r.raise_for_status()
    return r


def post(url, **kwargs):
    throttle()
    r = S.post(url, timeout=35, **kwargs)
    r.raise_for_status()
    return r


def collect_index(cutoff: datetime, end_at: datetime, max_pages: int = 120):
    found = {}
    for page in range(1, max_pages + 1):
        soup = BeautifulSoup(get(LIST_URL, params={"id": GALLERY_ID, "page": page}).text, "lxml")
        dates = []
        for row in soup.select("tr.ub-content"):
            no = row.get("data-no", "")
            date_el = row.select_one(".gall_date")
            link = row.select_one(".gall_tit a[href]")
            if not no.isdigit() or not date_el or not link or not date_el.get("title"):
                continue
            try:
                created = datetime.strptime(date_el["title"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                continue
            if row.get("data-type", "") not in {"icon_notice", "icon_fnews"}:
                dates.append(created)
            if cutoff <= created <= end_at and row.get("data-type", "") not in {"icon_notice", "icon_fnews"}:
                found[int(no)] = {"no": int(no), "title": clean(link.get_text(" ", strip=True)), "created_at": created.isoformat()}
        print(f"index page={page} posts={len(found)}", flush=True)
        if dates and max(dates) < cutoff:
            break
    return sorted(found.values(), key=lambda x: (x["created_at"], x["no"]), reverse=True)


def comments(post_no: int, referer: str, token: str):
    result = {}
    total = 0
    for page in range(1, 201):
        data = {"id": GALLERY_ID, "no": str(post_no), "cmt_id": GALLERY_ID, "cmt_no": str(post_no), "focus_cno": "", "focus_pno": "", "e_s_n_o": token, "comment_page": str(page), "sort": "I", "prevCnt": "", "board_type": "", "_GALLTYPE_": "M"}
        payload = post(COMMENT_URL, data=data, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}).json()
        total = max(total, int(payload.get("total_cnt") or 0))
        batch = payload.get("comments") or []
        if not batch:
            break
        before = len(result)
        for raw in batch:
            no = str(raw.get("no") or "")
            if no:
                result[no] = {"depth": int(raw.get("depth") or 0), "created_at": str(raw.get("reg_date") or ""), "text": soup_text(str(raw.get("memo") or "")), "deleted": str(raw.get("del_yn") or "N") == "Y"}
        if len(result) >= total or len(result) == before:
            break
    return list(result.values()), total


def collect_post(item):
    url = f"{VIEW_URL}?id={GALLERY_ID}&no={item['no']}"
    soup = BeautifulSoup(get(url, headers={"Referer": LIST_URL}).text, "lxml")
    title_el = soup.select_one(".gallview_head .title_subject")
    body_el = soup.select_one(".writing_view_box .write_div") or soup.select_one(".write_div")
    count_el = soup.select_one("#comment_cnt")
    expected = int(count_el.get("value", "0")) if count_el and count_el.get("value", "").isdigit() else 0
    c, total = [], expected
    errors = []
    if expected:
        token = soup.select_one("#e_s_n_o")
        if token and token.get("value"):
            try:
                c, total = comments(item["no"], url, token["value"])
            except Exception as exc:
                errors.append(f"댓글 수집 실패: {exc}")
        else:
            errors.append("댓글 토큰 없음")
    return {"no": item["no"], "title": clean(title_el.get_text(" ", strip=True)) if title_el else item["title"], "created_at": item["created_at"], "body": clean(body_el.get_text("\n", strip=True)) if body_el else "", "comments": c, "expected_comments": max(expected, total), "errors": errors}


def render_post(p, idx):
    lines = [f"## 게시글 {idx}", "", f"제목: {p['title']}", "", f"번호: {p['no']}", "", f"작성시각: {p['created_at']}", "", f"댓글 수: {len(p['comments'])}/{p['expected_comments']}", "", "본문:", "", p['body'] or "(텍스트 본문 없음)", "", "댓글:", ""]
    if not p["comments"]:
        lines += ["- 댓글 없음", ""]
    else:
        for c in p["comments"]:
            lines.append(f"{'  - 답글' if c['depth'] else '- 댓글'}{' [삭제됨]' if c['deleted'] else ''} ({c['created_at']}): {c['text'] or '(텍스트 없음)'}")
        lines.append("")
    if p["errors"]:
        lines += ["수집 경고:", ""] + [f"- {e}" for e in p["errors"]] + [""]
    lines += ["---", ""]
    return "\n".join(lines)


def model_call(prompt: str, max_tokens: int = 6000) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN unavailable")
    r = requests.post("https://models.github.ai/inference/chat/completions", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json={"model": "openai/gpt-4.1-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.25, "max_tokens": max_tokens}, timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def chunks(parts, limit=115000):
    out, cur, size = [], [], 0
    for part in parts:
        if cur and size + len(part) > limit:
            out.append("\n".join(cur)); cur, size = [], 0
        cur.append(part); size += len(part)
    if cur:
        out.append("\n".join(cur))
    return out


def fallback_html(manifest, summaries):
    cards = "".join(f"<article class='card'><h2>정보 묶음 {i}</h2><p>{html.escape(s).replace(chr(10), '<br>')}</p></article>" for i, s in enumerate(summaries, 1))
    return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>특이점 23H 브리핑</title><style>body{{margin:0;background:#050713;color:#eaf7ff;font-family:system-ui;line-height:1.7}}main{{max-width:760px;margin:auto;padding:24px}}.hero,.card{{background:linear-gradient(145deg,#101936,#07101f);border:1px solid #36f0ff55;border-radius:24px;padding:24px;margin:18px 0;box-shadow:0 0 28px #00d9ff18}}h1,h2{{color:#76f7ff}}.pill{{display:inline-block;padding:6px 12px;border-radius:999px;background:#7b35ff55}}</style></head><body><main><section class='hero'><span class='pill'>SINGULARITY 23H</span><h1>특이점 갤러리 종합 브리핑</h1><p>{manifest['post_count']}개 게시글, {manifest['comment_count']}개 댓글을 통합 분석했습니다.</p></section>{cards}</main></body></html>"


def main():
    out = Path("output"); out.mkdir(exist_ok=True)
    end_at = datetime.now(KST); cutoff = end_at - timedelta(hours=23)
    items = collect_index(cutoff, end_at)
    posts = []
    for i, item in enumerate(items, 1):
        try:
            posts.append(collect_post(item))
        except Exception as exc:
            posts.append({"no": item["no"], "title": item["title"], "created_at": item["created_at"], "body": "", "comments": [], "expected_comments": 0, "errors": [str(exc)]})
        if i % 25 == 0 or i == len(items):
            print(f"collected {i}/{len(items)}", flush=True)
    parts = [render_post(p, i) for i, p in enumerate(posts, 1)]
    manifest = {"cutoff_at": cutoff.isoformat(), "ended_at": end_at.isoformat(), "post_count": len(posts), "comment_count": sum(len(p["comments"]) for p in posts), "expected_comment_count": sum(p["expected_comments"] for p in posts), "posts_with_errors": sum(bool(p["errors"]) for p in posts)}
    archive = "# 특이점 갤러리 최근 23시간 원문 아카이브\n\n" + "\n".join(f"- {k}: {v}" for k, v in manifest.items()) + "\n\n" + "\n".join(parts)
    (out / "singularity_23h_archive.md").write_text(archive, encoding="utf-8")
    summaries = []
    for i, chunk in enumerate(chunks(parts), 1):
        prompt = """다음은 특이점 갤러리 최근 23시간 원문 중 한 묶음이다. 게시글 번호를 색인하거나 글별 목록을 만들지 말고, 모든 서로 다른 주장·뉴스·루머·반응·논쟁·예측을 빠뜨리지 않게 뉴스룸 메모처럼 통합 요약하라. 사실/추정/농담/커뮤니티 반응을 구분하고, 반복은 합쳐라. 한국어로 촘촘하게 작성하라.\n\n""" + chunk
        try:
            summaries.append(model_call(prompt, 6500))
        except Exception as exc:
            print(f"model chunk {i} failed: {exc}", flush=True)
            titles = [line[4:] for line in chunk.splitlines() if line.startswith("제목:")]
            summaries.append("이번 묶음의 주요 화제: " + ", ".join(titles))
        time.sleep(2)
    final_prompt = """아래는 특이점 갤러리 최근 23시간 전체를 나눠 분석한 뉴스룸 메모다. 이를 바탕으로 하나의 완성된 모바일 우선 HTML 웹페이지를 만들어라. 원문 게시글 색인이나 글 번호별 나열은 금지한다. 모든 메모 내용을 통합적으로 다루고 뉴스 기사처럼 읽히되 캐주얼하고 가독성 좋게 구성하라. 시간대별 게시량 분포는 넣지 마라. 진짜 특이점 느낌의 게임형 UI/UX, 네온·글래스 효과, 부드러운 애니메이션, 읽은 섹션 표시와 다음 섹션 이동 버튼을 넣어라. 외부 라이브러리·외부 이미지·외부 링크 없이 단일 HTML로 완결하라. 사실·루머·커뮤니티 정서를 구분하라. 응답은 코드펜스 없이 <!doctype html>부터 시작하는 HTML만 출력하라.\n\n수집 통계:\n""" + json.dumps(manifest, ensure_ascii=False) + "\n\n뉴스룸 메모:\n" + "\n\n===== 묶음 =====\n\n".join(summaries)
    try:
        page = model_call(final_prompt, 12000).strip()
        page = re.sub(r"^```html\s*|\s*```$", "", page, flags=re.I | re.S)
        if "<html" not in page.lower():
            raise RuntimeError("HTML response missing")
    except Exception as exc:
        print(f"final model failed: {exc}", flush=True)
        page = fallback_html(manifest, summaries)
    (out / "singularity_23h_news.html").write_text(page, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
