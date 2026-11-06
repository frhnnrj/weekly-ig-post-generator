#!/usr/bin/env python3
"""Weekly IG Post Generator for Rumah123.

Flow: RSS property news -> pick fresh articles -> LLM writes a caption
-> rule-based validator checks brand/compliance rules -> retry with feedback
-> save to output/posts_YYYY-MM-DD.{md,json}
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
DEFAULT_MODELS = {"gemini": "gemini-flash-latest", "anthropic": "claude-sonnet-4-6"}
MODEL = os.getenv("MODEL") or DEFAULT_MODELS.get(PROVIDER, "")
POSTS_PER_RUN = int(os.getenv("POSTS_PER_RUN", "3"))
MAX_ATTEMPTS = 3
MAX_AGE_DAYS = 7

BANKS = ["bca", "bri", "bni", "mandiri", "btn", "cimb", "permata", "danamon", "ocbc", "bsi", "maybank"]
COMPETITORS = ["99.co", "99 co", "olx", "lamudi", "dotproperty", "urbanindo", "rumah.com"]
PROMISES = ["pasti disetujui", "dijamin", "pasti acc", "tanpa syarat"]
ALLOWED_NUMBERS = {"20", "30"}  # DP saving range and installment cap from the brief


# ---------- news ----------
def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_news():
    import feedparser

    urls = [
        l.strip()
        for l in (ROOT / "feeds.txt").read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#")
    ]
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    seen, items = set(), []
    for url in urls:
        feed = feedparser.parse(url)
        for e in feed.entries:
            title = clean(e.get("title"))
            key = re.sub(r"\W+", "", title.lower())[:60]
            if not title or key in seen:
                continue
            ts = time.mktime(e.published_parsed) if e.get("published_parsed") else time.time()
            if ts < cutoff:
                continue
            seen.add(key)
            src = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
            items.append(
                {
                    "title": title,
                    "summary": clean(e.get("summary")),
                    "link": e.get("link", ""),
                    "source": src or feed.feed.get("title", "RSS"),
                    "published": e.get("published", ""),
                    "ts": ts,
                }
            )
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:POSTS_PER_RUN]


# ---------- LLM ----------
class FatalAPIError(Exception):
    """Config/auth/model errors that retrying cannot fix."""


def gemini_pick_model(key: str) -> str:
    """Ask the API which models this key can use and pick a current Flash model."""
    import requests

    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        params={"pageSize": 200},
        timeout=30,
    )
    r.raise_for_status()
    names = [
        m["name"].split("/")[-1]
        for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    if "gemini-flash-latest" in names:
        return "gemini-flash-latest"
    skip = ("lite", "preview", "exp", "image", "tts", "thinking", "live", "audio", "vision")
    flash = sorted(n for n in names if "flash" in n and not any(x in n for x in skip))
    if not flash:
        raise FatalAPIError(f"Tidak ada model Flash yang tersedia untuk key ini. Model: {names[:15]}")
    return flash[-1]


class TransientAPIError(Exception):
    """Overload/timeouts that persisted after all retries and fallbacks."""


def post_with_retry(url, headers, body, tries=4, timeout=60):
    """POST with exponential backoff on timeouts, 429 and 5xx."""
    import requests

    delay, last = 5, ""
    for i in range(1, tries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            last = f"HTTP {r.status_code}"
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = type(exc).__name__
        print(f"  transient error ({last}), attempt {i}/{tries}")
        if i < tries:
            time.sleep(delay)
            delay *= 2
    raise TransientAPIError(last)


def call_llm(system: str, user: str) -> str:
    global MODEL

    if PROVIDER == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.6, "responseMimeType": "application/json"},
        }
        fallbacks = [m.strip() for m in os.getenv("FALLBACK_MODELS", "gemini-flash-lite-latest").split(",")]
        candidates = [MODEL] + [m for m in fallbacks if m and m != MODEL]
        last_err = None
        idx = 0
        while idx < len(candidates):
            model = candidates[idx]
            idx += 1
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                r = post_with_retry(url, headers, body)
            except TransientAPIError as exc:
                last_err = exc
                print(f"Model '{model}' sibuk ({exc}); mencoba model cadangan.")
                continue
            if r.status_code == 404:
                print(f"Model '{model}' tidak ditemukan (404).")
                if idx == 1:  # primary model missing: discover what this key can use
                    picked = gemini_pick_model(key)
                    if picked not in candidates:
                        candidates.insert(idx, picked)
                continue
            if r.status_code in (400, 401, 403):
                raise FatalAPIError(f"Gemini API {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            MODEL = model
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        raise TransientAPIError(f"semua model gagal/sibuk: {last_err}")

    if PROVIDER == "anthropic":
        key = os.environ["ANTHROPIC_API_KEY"]
        r = post_with_retry(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            {
                "model": MODEL,
                "max_tokens": 1500,
                "temperature": 0.6,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        if r.status_code in (400, 401, 403, 404):
            raise FatalAPIError(f"Anthropic API {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])

    raise FatalAPIError(f"LLM_PROVIDER tidak dikenal: {PROVIDER}")


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


# ---------- validation ----------
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def validate(post: dict, article: dict) -> list:
    """Return a list of human-readable problems. Empty list = pass."""
    cap = post.get("caption", "")
    problems = []
    body = "\n".join(l for l in cap.splitlines() if not l.strip().startswith("#"))
    words = len(body.split())
    tags = re.findall(r"#\w+", cap)
    low = cap.lower()

    if not 120 <= words <= 180:
        problems.append(f"Panjang {words} kata; harus 120-180 kata di luar hashtag.")
    if not 6 <= len(tags) <= 8:
        problems.append(f"Ada {len(tags)} hashtag; harus 6-8.")
    n_emoji = len(EMOJI.findall(cap))
    if not 4 <= n_emoji <= 6:
        problems.append(f"Ada {n_emoji} emoji; harus 4-6.")
    n_brand = len(re.findall(r"rumah123", body, re.I))
    if n_brand != 1:
        problems.append(f"'Rumah123' muncul {n_brand}x di badan caption; harus tepat 1x.")
    source_low = f"{article['title']} {article['summary']}".lower()
    squash = lambda t: re.sub(r"[\s.,]", "", t)
    money = re.findall(
        r"\brp\.?\s?\d[\d.,]*(?:\s?(?:juta|miliar|triliun|ribu))?"
        r"|\b\d[\d.,]*\s?(?:juta|miliar|triliun)\b"
        r"|\b(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|seratus|ratus)\s+(?:ratus\s+)?(?:juta|miliar|triliun)\b",
        low,
    )
    bad_money = [m for m in money if squash(m) not in squash(source_low)]
    if bad_money:
        problems.append(f"Menyebut nominal rupiah yang tidak ada di sumber: {bad_money}. Hindari nominal, tulis secara umum.")
    if re.search(r"bunga[^.\n]{0,15}\d+(?:[.,]\d+)?\s?%", low):
        problems.append("Menyebut besaran suku bunga.")
    for name in BANKS:
        if re.search(rf"\b{re.escape(name)}\b", low):
            problems.append(f"Menyebut nama bank: {name}.")
    for name in COMPETITORS:
        if name in low:
            problems.append(f"Menyebut kompetitor: {name}.")
    for p in PROMISES:
        if p in low:
            problems.append(f"Janji persetujuan KPR: '{p}'.")

    source_text = f"{article['title']} {article['summary']}"
    # ignore list markers ("1.") and the brand name when checking numbers
    num_text = re.sub(r"^[\s*_>\-]*\d+\s*[.)]", "", body, flags=re.M)
    num_text = re.sub(r"rumah123", "", num_text, flags=re.I)
    for num in set(re.findall(r"\d+", num_text)):
        if num not in ALLOWED_NUMBERS and num not in source_text:
            problems.append(f"Angka '{num}' tidak ada di sumber berita (potensi halusinasi).")
    if not post.get("facts_used"):
        problems.append("facts_used kosong.")
    return problems


# ---------- pipeline ----------
def generate_post(article: dict, system: str, user_tpl: str) -> dict:
    user = (
        user_tpl.replace("{title}", article["title"])
        .replace("{source}", article["source"])
        .replace("{published}", article["published"])
        .replace("{summary}", article["summary"] or "(tidak ada ringkasan, gunakan judul saja)")
    )
    problems, post = [], {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = user
        if problems:
            prompt += "\n\nDraf sebelumnya ditolak validator karena:\n- " + "\n- ".join(problems)
            prompt += "\nPerbaiki semuanya dan balas JSON saja."
        try:
            post = parse_json(call_llm(system, prompt))
        except FatalAPIError:
            raise
        except TransientAPIError as exc:
            problems = [f"API tidak tersedia setelah retry dan model cadangan: {exc}"]
            break
        except Exception as exc:  # bad JSON / API hiccup -> retry
            problems = [f"Output bukan JSON valid atau API error: {exc}"]
            print(f"  percobaan {attempt} error: {exc}")
            continue
        problems = validate(post, article)
        if problems:
            print(f"  percobaan {attempt} ditolak: {problems}")
            print(f"  caption ditolak (potongan): {str(post.get('caption', ''))[:500]!r}")
        if not problems:
            post["attempts"] = attempt
            post["validated"] = True
            return post
    post["attempts"] = MAX_ATTEMPTS
    post["validated"] = False
    post["problems"] = problems
    return post


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="hanya tampilkan berita yang terpilih")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    articles = fetch_news()
    if not articles:
        sys.exit("Tidak ada berita baru dalam 7 hari terakhir. Cek feeds.txt.")
    if args.dry_run:
        for a in articles:
            print(f"- {a['title']} ({a['source']})")
        return

    system = (ROOT / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    user_tpl = (ROOT / "prompts" / "user_prompt.md").read_text(encoding="utf-8")

    results = []
    for a in articles:
        try:
            post = generate_post(a, system, user_tpl)
        except FatalAPIError as exc:
            sys.exit(f"ERROR FATAL (tidak ada gunanya diulang): {exc}")
        post["source_title"], post["source_url"] = a["title"], a["link"]
        results.append(post)
        print(f"[{'OK' if post['validated'] else 'GAGAL'}] {a['title'][:70]} (percobaan {post['attempts']})")

    today = dt.date.today().isoformat()
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    (out / f"posts_{today}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# Draf postingan IG Rumah123, {today}\n"]
    for i, p in enumerate(results, 1):
        md.append(f"## Post {i}: {p['source_title']}")
        md.append(f"Sumber: {p['source_url']}")
        md.append(f"Validasi: {'lulus' if p['validated'] else 'GAGAL: ' + '; '.join(p.get('problems', []))} (percobaan ke-{p['attempts']})\n")
        md.append(p.get("caption", "(kosong)"))
        md.append("\n---\n")
    (out / f"posts_{today}.md").write_text("\n".join(md), encoding="utf-8")
    if not any(p.get("validated") for p in results):
        sys.exit("Tidak ada caption yang lolos validasi; run ditandai gagal.")


if __name__ == "__main__":
    main()
