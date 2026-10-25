#!/usr/bin/env python3
"""Shared Telegram sender for the bridge, the Claude Code Stop hook, and the
telegram MCP tool. Stdlib only. Proxy support mirrors the historic behaviour:

  1. TELEGRAM_HTTP_PROXY (explicit; set to "none" to force direct)
  2. HTTP_PROXY / HTTPS_PROXY / NO_PROXY auto-detection (urllib.getproxies)

Deployed copy lives at ~/.claude/telegram_lib.py; the canonical source is in
inside the claude_tg package in the claude-tg repo.
"""
import os
import re
import sys
import time
import json
import urllib.request

CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
MAX_PART = 3800          # split size; Telegram's hard limit is 4096 after conversion
TELEGRAM_PROXY = os.environ.get("TELEGRAM_HTTP_PROXY", "").strip()


def build_opener():
    """urllib opener honouring TELEGRAM_HTTP_PROXY / standard proxy env vars.
    Reads the environment at call time, so live updates work."""
    proxy = os.environ.get("TELEGRAM_HTTP_PROXY", "").strip()
    if proxy.lower() == "none":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        proxies = {"http": proxy, "https": proxy}
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    proxies = urllib.request.getproxies()
    if proxies:
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener()


def reload_proxy():
    """Rebuild the module opener from the current environment (live updates)."""
    global _OPENER
    _OPENER = build_opener()


_OPENER = build_opener()


def read_chat_id():
    try:
        with open(CHAT_ID_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def md_to_html(text):
    """Markdown -> Telegram HTML. Protects code/links before escaping; handles
    emphasis, headings, blockquotes, tables (as <pre>), lists, hr.
    Provenance: written in this fork (pre-git send-to-telegram.sh era) — it is
    NOT part of upstream hanxiao/claudecode-telegram, whose hook had no
    converter."""
    esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    blocks, inlines, links = [], [], []
    text = re.sub(r'```(\w*)\n?(.*?)```',
                  lambda m: (blocks.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks)-1}\x00")[1],
                  text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`',
                  lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1], text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
                  lambda m: (links.append((m.group(1), m.group(2))), f"\x00L{len(links)-1}\x00")[1], text)
    text = esc(text)
    # Inline emphasis (order matters: ** before *, __ after _)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<![\w*])\*([^*\n]+)\*(?![\w*])', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    # Line-based constructs (blockquote marker is &gt; after escaping)
    lines = text.split('\n')
    res, in_q, i, n = [], False, 0, len(lines)
    while i < n:
        s = lines[i].lstrip()
        m = re.match(r'^#{1,6}\s+(.*)$', s)
        if m:
            if in_q:
                res.append('</blockquote>'); in_q = False
            res.append('<b>' + m.group(1) + '</b>')
        elif s.startswith('&gt;'):
            if not in_q:
                res.append('<blockquote>'); in_q = True
            res.append(re.sub(r'^(?:&gt;)+\s?', '', s))
        elif s.startswith('|'):
            if in_q:
                res.append('</blockquote>'); in_q = False
            tbl = []
            while i < n and lines[i].lstrip().startswith('|'):
                row = lines[i].lstrip()
                if not re.match(r'^\|[\s:|\-]+\|?$', row):
                    def _ph(mm):
                        # cells render as <pre>: restore inline/block code as
                        # escaped text instead of dropping the placeholder
                        raw = inlines[int(mm.group(2))] if mm.group(1) == 'I' else blocks[int(mm.group(2))][1]
                        return esc(raw)
                    row = re.sub(r'\x00L(\d+)\x00', lambda mm: esc(links[int(mm.group(1))][0]), row)
                    row = re.sub(r'\x00([BI])(\d+)\x00', _ph, row)
                    tbl.append(re.sub(r'</?(?:b|i|u|s)>', '', row))
                i += 1
            res.append('<pre>' + '\n'.join(tbl) + '</pre>')
            continue
        else:
            if in_q:
                res.append('</blockquote>'); in_q = False
            if re.match(r'^[-*+]\s+', s):
                res.append('• ' + re.sub(r'^[-*+]\s+', '', s))
            elif s.strip() in ('---', '***', '___'):
                res.append('─' * 12)
            else:
                res.append(lines[i])
        i += 1
    if in_q:
        res.append('</blockquote>')
    text = '\n'.join(res)
    # Reinsert protected content
    for i, (lang, code) in enumerate(blocks):
        cls = f' class="language-{lang}"' if lang else ''
        text = text.replace(f"\x00B{i}\x00", f'<pre><code{cls}>{esc(code.strip())}</code></pre>')
    for i, code in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')
    for i, (label, url) in enumerate(links):
        text = text.replace(f"\x00L{i}\x00", f'<a href="{esc(url)}">{esc(label)}</a>')
    return text


def split_message(text, max_part=MAX_PART):
    """Split at line boundaries; hard-splits oversized lines. Returns non-empty parts."""
    if len(text) <= max_part:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_part:
            take = max_part - len(current)
            parts.append(current + line[:take])
            current, line = "", line[take:]
        if len(current) + len(line) > max_part:
            parts.append(current)
            current = line
        else:
            current += line
    if current.strip():
        parts.append(current)
    return [p for p in (s.strip("\n") for s in parts) if p]


def _send_api(payload, token):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=15) as r:
            return json.loads(r.read()).get("ok")
    except Exception as e:
        sys.stderr.write(f"sendMessage failed: {e}\n")
        return False


def send_text(text, chat_id, token, parse_mode=None, thread=None):
    """Send one message with optional parse mode / forum topic thread."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread:
        payload["message_thread_id"] = thread
    return _send_api(payload, token)


def send_markdown(text, chat_id, token, header=None, thread=None):
    """Markdown in, delivered parts out. Returns (sent, total, errors[]).
    The header is stamped on every part, so a reply split across several
    messages stays self-identifying (each part names its session)."""
    text = (text or "").strip()
    if not text or text == "null":
        return 0, 0, ["empty text"]
    parts = split_message(text)
    errors, sent = [], 0
    for i, part in enumerate(parts):
        if header:
            part = f"{header}\n{part}"
        body = f"({i + 1}/{len(parts)})\n{part}" if len(parts) > 1 else part
        ok = send_text(md_to_html(body), chat_id, token, parse_mode="HTML", thread=thread)
        if not ok:
            # Telegram rejected the HTML — fall back to the original markdown,
            # not to the converted string (that would show literal <b> tags)
            payload = {"chat_id": chat_id, "text": body[:4096]}
            if thread:
                payload["message_thread_id"] = thread
            ok = _send_api(payload, token)
        if ok:
            sent += 1
        else:
            errors.append(f"part {i + 1}/{len(parts)} failed")
        if i < len(parts) - 1:
            time.sleep(0.4)  # pacing keeps Telegram's per-chat rate limits happy
    return sent, len(parts), errors
