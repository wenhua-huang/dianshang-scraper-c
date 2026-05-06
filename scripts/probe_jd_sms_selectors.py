from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


KEYWORDS = [
    "验证码",
    "短信",
    "获取",
    "发送",
    "重新发送",
    "发送短信验证码",
    "获取验证码",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe SMS-code button selectors on JD pages")
    parser.add_argument("--url", required=True, help="Target JD verification URL")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222", help="Chrome CDP endpoint")
    parser.add_argument("--wait-ms", type=int, default=2000, help="Wait after navigation")
    parser.add_argument(
        "--out-dir",
        default="artifacts/debug/jd_sms_selector_probe",
        help="Output directory",
    )
    return parser


def _make_selector(tag: str, element_id: str, classes: list[str], text: str) -> list[str]:
    candidates: list[str] = []
    text = (text or "").strip()

    if element_id:
        candidates.append(f"#{element_id}")
    if classes:
        safe_classes = [c for c in classes if c and " " not in c]
        if safe_classes:
            candidates.append(tag + "".join(f".{c}" for c in safe_classes[:3]))
            low = " ".join(safe_classes).lower()
            if "sms" in low:
                candidates.append("[class*='sms']")
            if "verify" in low:
                candidates.append("[class*='verify']")
            if "code" in low:
                candidates.append("[class*='code']")
            if "send" in low:
                candidates.append("[class*='send']")
            if "get" in low or "fetch" in low:
                candidates.append("[class*='get'], [class*='fetch']")

    if text:
        if len(text) <= 20:
            candidates.append(f"{tag}:has-text('{text}')")
            candidates.append(f"text={text}")
        if "验证码" in text:
            candidates.append(f"{tag}:has-text('验证码')")
        if "发送" in text:
            candidates.append(f"{tag}:has-text('发送')")
        if "获取" in text:
            candidates.append(f"{tag}:has-text('获取')")

    # de-dup while preserving order
    uniq: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def main() -> int:
    args = build_parser().parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = int(time.time())
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(max(0, args.wait_ms))

        data: dict[str, Any] = page.evaluate(
            r"""
            (keywords) => {
              const frames = [window, ...Array.from(window.frames || [])];
              const hitNodes = [];

              function nodeInfo(el, frameIndex) {
                const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const attrs = {};
                for (const name of ['id', 'name', 'type', 'role', 'aria-label', 'placeholder', 'onclick', 'data-role', 'data-type']) {
                  const v = el.getAttribute(name);
                  if (v) attrs[name] = v;
                }
                return {
                  frame_index: frameIndex,
                  tag: (el.tagName || '').toLowerCase(),
                  text: txt,
                  class_name: (el.className || '').toString(),
                  id: el.id || '',
                  visible: !!(rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'),
                  disabled: !!el.disabled,
                  attrs,
                };
              }

              for (let i = 0; i < frames.length; i++) {
                let doc;
                try {
                  doc = frames[i].document;
                } catch {
                  continue;
                }
                if (!doc || !doc.body) continue;

                const elems = doc.querySelectorAll('button, a, span, div, input[type="button"], input[type="submit"], [role="button"], [onclick], [class*="sms"], [class*="code"], [class*="verify"], [id*="sms"], [id*="code"]');
                for (const el of elems) {
                  const txt = (el.innerText || el.textContent || el.getAttribute('value') || '').replace(/\s+/g, ' ').trim();
                  const cls = (el.className || '').toString();
                  const id = (el.id || '').toString();
                  const onclick = (el.getAttribute('onclick') || '').toString();
                  const haystack = [txt, cls, id, onclick].join(' ').toLowerCase();
                  if (!haystack) continue;
                  if (!keywords.some(k => haystack.includes(k.toLowerCase()))) continue;
                  hitNodes.push(nodeInfo(el, i));
                }
              }

              return {
                url: location.href,
                title: document.title,
                hits: hitNodes,
              };
            }
            """,
            KEYWORDS,
        )

        screenshot_path = out_dir / f"probe_{ts}.png"
        html_path = out_dir / f"probe_{ts}.html"
        json_path = out_dir / f"probe_{ts}.json"

        page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(page.content(), encoding="utf-8")

        enriched_hits: list[dict[str, Any]] = []
        for idx, hit in enumerate(data.get("hits", []), start=1):
            tag = str(hit.get("tag") or "").lower() or "button"
            classes = [c for c in str(hit.get("class_name") or "").split() if c]
            selectors = _make_selector(
                tag=tag,
                element_id=str(hit.get("id") or ""),
                classes=classes,
                text=str(hit.get("text") or ""),
            )
            enriched_hit = dict(hit)
            enriched_hit["index"] = idx
            enriched_hit["candidate_selectors"] = selectors
            enriched_hits.append(enriched_hit)

        report = {
            "url": data.get("url"),
            "title": data.get("title"),
            "hit_count": len(enriched_hits),
            "hits": enriched_hits,
            "artifacts": {
                "screenshot": str(screenshot_path),
                "html": str(html_path),
                "json": str(json_path),
            },
        }
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps(report, ensure_ascii=False, indent=2))
        page.close()
        if not browser.contexts:
            context.close()
        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
