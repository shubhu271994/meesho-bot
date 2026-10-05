#!/usr/bin/env python3
"""
Meesho Supplier Panel order bot.

Runs for every account listed in .env, one after another. For each account it
logs in (reusing a saved session when possible), accepts all pending orders,
then downloads shipping labels + manifest for Ready-to-Ship orders.
Sends a run summary to Slack (optional).

Usage:
  python bot.py                 # normal run (headless)
  python bot.py --dry-run       # select orders + download labels/manifest, but never click Accept
  python bot.py --headed        # show the browser (local testing only)
  python bot.py --login-only    # just log in and save the session
  python bot.py --account ruchika   # only one account
"""
import argparse
import datetime as dt
import fcntl
import subprocess
import time
import json
import logging
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
CFG = yaml.safe_load((BASE / "config.yaml").read_text())

LOCK_FILE = BASE / "state" / "bot.lock"
RUN_ID = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
RUN_DIR = BASE / "runs" / RUN_ID

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

T = CFG["text"]          # visible-text labels used to find buttons/tabs
TIMEOUT = CFG.get("timeout_ms", 30000)

log = logging.getLogger("meesho-bot")


def load_accounts():
    """Reads ACCOUNT_1_LOGIN / ACCOUNT_1_PASSWORD / ACCOUNT_1_NAME, ACCOUNT_2_…, from .env."""
    accts, i = [], 1
    while os.environ.get(f"ACCOUNT_{i}_LOGIN"):
        accts.append({
            "name": os.environ.get(f"ACCOUNT_{i}_NAME") or f"account{i}",
            "login": os.environ[f"ACCOUNT_{i}_LOGIN"],
            "password": os.environ.get(f"ACCOUNT_{i}_PASSWORD", ""),
        })
        i += 1
    return accts


# Per-account values, set by use_account() before each account is processed
LOGIN_ID = PASSWORD = ACCT = ""
STATE_FILE = BASE / "state" / "auth.json"
ACCT_DIR = RUN_DIR


def use_account(a):
    global LOGIN_ID, PASSWORD, ACCT, STATE_FILE, ACCT_DIR
    LOGIN_ID, PASSWORD, ACCT = a["login"], a["password"], a["name"]
    STATE_FILE = BASE / "state" / f"auth_{ACCT}.json"
    ACCT_DIR = RUN_DIR / ACCT
    ACCT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- helpers
def setup_logging():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(RUN_DIR / "run.log")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def snap(page, name):
    """Screenshot + HTML dump for debugging."""
    try:
        page.screenshot(path=str(ACCT_DIR / f"{name}.png"), full_page=True)
        (ACCT_DIR / f"{name}.html").write_text(page.content())
    except Exception:
        pass


def rx(words):
    """Case-insensitive regex matching any of the given labels."""
    if isinstance(words, str):
        words = [words]
    return re.compile("|".join(re.escape(w) for w in words), re.I)


def safe_click(page, loc):
    """Click; if a promo popup is in the way, close it and try again."""
    try:
        loc.click(timeout=8000)
    except PWTimeout:
        dismiss_popups(page)
        loc.click(timeout=8000)


def click_text(page, words, role="button", required=True, timeout=None):
    loc = page.get_by_role(role, name=rx(words)).first
    try:
        loc.wait_for(state="visible", timeout=timeout or TIMEOUT)
        safe_click(page, loc)
        return True
    except PWTimeout:
        # fall back to any element with that text
        alt = page.get_by_text(rx(words)).first
        if alt.count() and alt.is_visible():
            safe_click(page, alt)
            return True
        if required:
            raise RuntimeError(f"Could not find {role} with text {words}")
        return False


# Pop-ups holding one of these buttons belong to the bot's own steps — never auto-close them
ACTION_BTN = r"^(labels?|download\s+labels?|accept[a-z ]*|confirm|yes[a-z ,]*|manifest|download\s+manifest)$"
# Harmless "dismiss" buttons that are safe to click on any other pop-up
ACK_BTN = r"^(got it!?|okay|ok|close|skip|not now|later|maybe later|dismiss|understood|done|i understand)$"


FIND_POPUP_JS = r"""
(args) => {
  const [sel, actRe, ackRe] = args;
  const act = new RegExp(actRe, 'i');   // buttons the bot itself needs (Label, Accept, Confirm…)
  const ack = new RegExp(ackRe, 'i');   // harmless dismiss buttons (Got it, Okay, Close…)
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05; };
  const vw = innerWidth, vh = innerHeight;
  let roots = [...document.querySelectorAll(sel)];
  for (const el of document.body.querySelectorAll('div,section,aside')) {
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' && +cs.zIndex >= 10) {      // full-screen overlay layer
      const r = el.getBoundingClientRect();
      if (r.width > vw * 0.6 && r.height > vh * 0.6) roots.push(el);
    }
  }
  roots = [...new Set(roots)].filter(vis);
  const z = el => { let v = 0; for (let e = el; e && e !== document.body; e = e.parentElement) {
      const zi = parseInt(getComputedStyle(e).zIndex); if (!isNaN(zi)) v = Math.max(v, zi); } return v; };
  roots.sort((a, b) => (z(b) - z(a)) ||
    ((a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? 1 : -1));   // top-most pop-up first
  const onTop = (root, x, y) => { const h = document.elementFromPoint(x, y); return h && (h === root || root.contains(h)); };
  for (const root of roots) {
    // skip dropdown menus / account menus / select lists
    if (root.querySelector('[role=menu],[role=listbox],[role=menuitem],[role=option]')) continue;
    const txt = (root.innerText || '').trim();
    if (!txt) continue;
    const btns = [...root.querySelectorAll('button,[role=button],a')].filter(el =>
      vis(el) && el.childElementCount <= 2 && (el.innerText || '').trim().length > 0 && (el.innerText || '').trim().length < 30);
    if (btns.some(b => act.test((b.innerText || '').trim()))) continue;   // bot's own action pop-up
    const ackBtn = btns.find(b => ack.test((b.innerText || '').trim()));
    // backdrop point: a spot on the dark overlay outside the card (only if the overlay is really there)
    const backdrop = () => {
      let ov = root; for (let e = root; e && e !== document.body; e = e.parentElement) {
        const r = e.getBoundingClientRect(); if (getComputedStyle(e).position === 'fixed' && r.width >= vw * 0.9 && r.height >= vh * 0.9) { ov = e; break; } }
      const r = ov.getBoundingClientRect();
      if (r.width < vw * 0.9 || r.height < vh * 0.9) return null;
      for (const [px, py] of [[vw - 15, vh / 2], [vw / 2, vh - 15], [vw - 15, 15], [vw / 2, 15]]) {
        const h = document.elementFromPoint(px, py);
        if (h === ov) return [px, py];                    // hits the overlay itself, nothing underneath
      }
      return null;
    };
    const bd = backdrop();
    const res = (x, y) => ({text: txt.slice(0, 60), x, y, bx: bd ? bd[0] : null, by: bd ? bd[1] : null});
    if (ackBtn) { const r = ackBtn.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (onTop(root, x, y)) return res(x, y); }
    // the pop-up card = smallest element that still holds ~all the text and is big enough
    let card = null, cardArea = 1e12;
    for (const el of [root, ...root.querySelectorAll('div,section')]) {
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 300 || r.height < 180) continue;
      if ((el.innerText || '').trim().length < txt.length * 0.8) continue;
      const area = r.width * r.height;
      if (area < cardArea) { card = el; cardArea = area; }
    }
    if (!card) { if (bd) return res(null, null); continue; }  // too small to be a promo pop-up
    const c = card.getBoundingClientRect();
    if (c.width * c.height > vw * vh * 0.9) continue;         // that's the page, not a pop-up
    // ✕ candidates: small, no text, inside the card's top-right corner
    const cands = [...card.querySelectorAll('button,[role=button],svg,img,span,div,i,a')].filter(el => {
      if (!vis(el)) return false;
      const r = el.getBoundingClientRect();
      if (r.width > 64 || r.height > 64 || r.width < 8 || r.height < 8) return false;
      if ((el.innerText || '').trim().replace(/[×✕✖xX]/g, '')) return false;
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      return cx > c.left + c.width * 0.7 && cy < c.top + Math.min(120, c.height * 0.3);
    });
    const label = el => (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('data-testid') || '') + ' ' +
      (typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '');
    let best = cands.find(el => /close|dismiss|cross/i.test(label(el)));
    if (!best) { let bs = -1e9; for (const el of cands) { const r = el.getBoundingClientRect();
      const sc = r.right - r.top; if (sc > bs) { bs = sc; best = el; } } }
    if (best) { const r = best.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (onTop(root, x, y)) return res(x, y); }
    return res(null, null);
  }
  return null;
}
"""


def _find_popup(page):
    try:
        return page.evaluate(FIND_POPUP_JS, [CFG["selectors"]["popup"], ACTION_BTN, ACK_BTN])
    except Exception:
        return None


GOT_IT_RX = re.compile(r"^\s*(got it!?|okay|ok|understood|i understand|done)\s*$", re.I)


def click_got_it(page):
    """Fast path: any visible 'Got it' / 'Okay' button on a pop-up → click it."""
    n = 0
    for _ in range(3):
        loc = page.get_by_role("button", name=GOT_IT_RX)
        hit = False
        for k in range(min(loc.count(), 5)):
            el = loc.nth(k)
            try:
                if el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(500)
                    n += 1
                    hit = True
                    break
            except Exception:
                pass
        if not hit:
            break
    return n


CLOSE_X_JS = r"""
(root) => {
  const c = root.getBoundingClientRect();
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  let best = null, bs = -1e9;
  for (const el of root.querySelectorAll('button,[role=button],svg,img,span,div,i,a,[aria-label]')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 64 || r.height > 64 || r.width < 8 || r.height < 8) continue;
    if ((el.innerText || '').trim().replace(/[×✕✖xX]/g, '')) continue;
    if (/close|dismiss|cross/i.test((el.getAttribute('aria-label') || '') + (el.getAttribute('data-testid') || ''))) {
      return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }
    const sc = r.right - r.top * 1.5;
    if (sc > bs) { bs = sc; best = r; }
  }
  return best ? {x: best.left + best.width / 2, y: best.top + best.height / 2} : null;
}
"""


def close_known_popups(page):
    """Hard-coded Meesho pop-ups from config.yaml → known_popups."""
    n = 0
    for rule in CFG.get("known_popups", []):
        text, btn = rule["text"], rule["click"]
        for _ in range(2):
            pops = page.locator(CFG["selectors"]["popup"]).filter(has_text=re.compile(re.escape(text), re.I))
            target = None
            for k in range(pops.count()):
                if pops.nth(k).is_visible():
                    target = pops.nth(k)
            if target is None:
                break
            try:
                if btn.upper() == "X":
                    pos = target.evaluate(CLOSE_X_JS)
                    if pos:
                        page.mouse.click(pos["x"], pos["y"])
                    else:
                        page.keyboard.press("Escape")
                else:
                    target.get_by_role("button", name=re.compile(rf"^\s*{re.escape(btn)}\s*$", re.I)).first.click(timeout=3000)
                page.wait_for_timeout(600)
                n += 1
                log.info("Pop-up '%s' → %s", text, btn)
            except Exception as e:
                log.info("Known pop-up '%s' — couldn't press %s (%s)", text, btn, str(e)[:60])
                break
    return n


def dismiss_popups(page, rounds=4):
    """Close promo pop-ups (e.g. 'Opt-in Now! / Participate Now') by clicking their ✕.
    Verifies each one actually went away; never clicks Participate / Opt-in."""
    closed = close_known_popups(page) + click_got_it(page)
    for _ in range(rounds):
        pop = _find_popup(page)
        if not pop:
            break
        tries = []
        if pop.get("x") is not None:
            tries.append(lambda: page.mouse.click(pop["x"], pop["y"]))
        tries.append(lambda: page.keyboard.press("Escape"))
        if pop.get("bx") is not None:                          # dark overlay ("Close modal"), verified on top
            tries.append(lambda: page.mouse.click(pop["bx"], pop["by"]))
        gone = False
        for t in tries:
            try:
                t()
            except Exception:
                pass
            page.wait_for_timeout(700)
            after = _find_popup(page)
            if not after or after.get("text") != pop.get("text"):
                gone = True
                break
        if gone:
            closed += 1
        else:
            snap(page, "popup_not_closed")
            log.warning("Could not close pop-up: %r (see popup_not_closed.png)", pop.get("text"))
            break
    if closed:
        log.info("Closed %d pop-up(s).", closed)
    return closed


def settle(page, ms=1500):
    try:
        page.wait_for_load_state("networkidle", timeout=CFG.get("idle_wait_ms", 6000))
    except PWTimeout:
        pass
    page.wait_for_timeout(ms)
    dismiss_popups(page)


def notify(text):
    log.info("SUMMARY: %s", text.replace("\n", " | "))
    if SLACK_WEBHOOK:
        try:
            requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=15)
        except Exception as e:
            log.warning("Slack notify failed: %s", e)


def cleanup_old_runs():
    keep_days = CFG.get("keep_run_days", 14)
    cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
    for d in (BASE / "runs").glob("*"):
        try:
            if dt.datetime.strptime(d.name, "%Y-%m-%d_%H%M") < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except ValueError:
            pass


# ---------------------------------------------------------------- login
def on_login_page(page):
    pw = page.locator("input[type=password]").first
    return pw.count() > 0 and pw.is_visible()


def login(page, context):
    if not LOGIN_ID or not PASSWORD:
        raise RuntimeError(f"Login or password missing in .env for {ACCT}")
    log.info("Logging in…")
    page.goto(CFG["urls"]["login"], timeout=TIMEOUT)
    settle(page)
    id_box = page.locator(CFG["selectors"]["login_id"]).first
    id_box.wait_for(state="visible", timeout=TIMEOUT)
    id_box.fill(LOGIN_ID)
    page.locator(CFG["selectors"]["password"]).first.fill(PASSWORD)
    # Click the real password "Log in" submit button — NOT "Get OTP to log in",
    # which Meesho shows as soon as a mobile number is typed.
    submit = page.locator(CFG["selectors"]["login_submit"]).first
    if submit.count():
        submit.click()
    else:
        page.get_by_role("button", name=re.compile(r"^\s*log\s*in\s*$", re.I)).first.click()
    try:
        page.wait_for_url(lambda u: "login" not in u.lower(), timeout=TIMEOUT * 2)
    except PWTimeout:
        snap(page, "login_failed")
        raise RuntimeError("Login did not complete (wrong password, captcha or OTP?). See login_failed.png")
    settle(page)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(STATE_FILE))
    os.chmod(STATE_FILE, 0o600)
    log.info("Logged in, session saved.")


def ensure_logged_in(page, context):
    page.goto(CFG["urls"]["home"], timeout=TIMEOUT)
    settle(page)
    if on_login_page(page):
        login(page, context)
    else:
        log.info("Reused saved session.")
    global HOME_URL
    HOME_URL = page.url   # e.g. …/panel/v3/new/growth/<code>/home


# ---------------------------------------------------------------- orders
# Flow (as used on the panel):
#   Orders → Manage Orders → Pending tab → header select-all ☐ → Accept All
#   → wait until "Pending (xx)" becomes 0
#   → Ready to Ship tab → close pop-ups → if a "past labels" Label button is shown, click it
#   → header select-all ☐ → Label button (bottom-right) → save the PDF
HOME_URL = ""
ORDERS_URL = ""
HEADED = False


def pause_to_show(page, secs=4):
    """In --headed mode, hold so you can see what got selected."""
    if HEADED:
        page.wait_for_timeout(secs * 1000)


def click_exact_text(page, label, timeout=6000):
    """Click a visible element whose own text is exactly `label` (case-insensitive)."""
    loc = page.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
    end = timeout
    while end > 0:
        for k in range(loc.count()):
            el = loc.nth(k)
            try:
                if el.is_visible():
                    safe_click(page, el)
                    return True
            except Exception:
                pass
        page.wait_for_timeout(500)
        end -= 500
    return False


def go_to_orders(page):
    """Orders (sidebar) → Manage Orders."""
    global ORDERS_URL
    url = CFG["urls"].get("orders") or ORDERS_URL
    if url:
        page.goto(url, timeout=TIMEOUT)
        settle(page)
        return
    page.goto(HOME_URL or CFG["urls"]["home"], timeout=TIMEOUT)
    settle(page)
    before = page.url
    for name in T["orders_nav"]:                 # "Orders" in the sidebar (expands the menu)
        if click_exact_text(page, name):
            break
    page.wait_for_timeout(1000)
    for name in T["manage_orders_nav"]:          # "Manage Orders" in the submenu
        if click_exact_text(page, name):
            break
    settle(page)
    if page.url == before:                       # fallback: Home → To do list → Pending Orders card
        card = page.get_by_text(rx(T["pending_card"])).first
        if card.count() and card.is_visible():
            safe_click(page, card)
            settle(page)
    if page.url == before:
        snap(page, "orders_nav_failed")
        raise RuntimeError("Couldn't open Orders → Manage Orders — paste its URL into config.yaml → urls.orders")
    ORDERS_URL = page.url.split("?")[0]
    log.info("Orders page: %s", ORDERS_URL)


def words_rx(text):
    """'Ready to Ship' → pattern that also matches non-breaking / multiple spaces and any case."""
    return r"[\s\u00a0\u200b]+".join(re.escape(w) for w in text.split())


def open_tab(page, tab_name):
    """Click a tab like 'Pending (12)' / 'Ready to Ship (5)'."""
    pat = re.compile(rf"^[\s\u00a0]*{words_rx(tab_name)}[\s\u00a0]*(\([\s\u00a0]*\d+[\s\u00a0]*\))?[\s\u00a0]*$", re.I)
    for role in ("tab", None):
        loc = page.get_by_role("tab", name=pat) if role else page.get_by_text(pat)
        for k in range(loc.count()):
            el = loc.nth(k)
            if el.is_visible():
                safe_click(page, el)
                settle(page)
                return True
    log.warning("Tab '%s' not found.", tab_name)
    return False


def tab_count(page, tab_name):
    """Read xx from the tab label 'Pending (xx)' / 'Ready to Ship (xx)'. None if not shown."""
    pat = re.compile(rf"^[\s\u00a0]*{words_rx(tab_name)}[\s\u00a0]*\(?[\s\u00a0]*(\d+)[\s\u00a0]*\)?[\s\u00a0]*$", re.I)
    try:
        for t in page.get_by_role("tab").all_inner_texts():
            m = pat.match(t)
            if m:
                return int(m.group(1))
        cands = page.get_by_text(re.compile(rf"^[\s\u00a0]*{words_rx(tab_name)}", re.I))
        for k in range(min(cands.count(), 10)):
            m = pat.match(cands.nth(k).inner_text(timeout=2000))
            if m:
                return int(m.group(1))
        body = page.inner_text("body", timeout=5000)
    except Exception:
        return None
    m = re.search(rf"{words_rx(tab_name)}[\s\u00a0]*\([\s\u00a0]*(\d+)[\s\u00a0]*\)", body, re.I)
    return int(m.group(1)) if m else None


def header_checkbox(page):
    """The select-all ☐ in the table header row (the row with 'Product Details', 'Sub-order ID' …)."""
    anchor = T["table_header_anchor"]
    xp = (f"xpath=//*[normalize-space(text())='{anchor}']"
          "/ancestor::*[.//input[@type='checkbox'] or .//*[@role='checkbox']][1]")
    row = page.locator(xp).first
    if row.count():
        box = row.locator("input[type=checkbox], [role=checkbox]").first
        if box.count():
            return box
    box = page.locator(CFG["selectors"]["select_all"]).first
    return box if box.count() else None


HEADER_BOX_JS = r"""
(anchor) => {
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const heads = [...document.querySelectorAll('body *')].filter(el =>
    el.childElementCount === 0 && (el.textContent || '').trim() === anchor && vis(el));
  for (const h of heads) {
    const hr = h.getBoundingClientRect();
    // small square-ish element left of "Product Details", on the same line
    let best = null, bestDx = 1e9;
    for (const el of document.querySelectorAll('input,span,div,svg,label,button,[role=checkbox]')) {
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8 || r.width > 48 || r.height > 48) continue;
      if (r.right > hr.left + 2) continue;
      const cy = r.top + r.height / 2, hy = hr.top + hr.height / 2;
      if (Math.abs(cy - hy) > 20) continue;
      const dx = hr.left - r.right;
      if (dx < bestDx) { bestDx = dx; best = r; }
    }
    if (best) return {x: best.left + best.width / 2, y: best.top + best.height / 2};
  }
  return null;
}
"""


def wait_for_table(page, secs=25):
    """Wait until the orders table header ('Product Details') is on screen."""
    anchor = page.get_by_text(re.compile(rf"^\s*{re.escape(T['table_header_anchor'])}\s*$", re.I)).first
    try:
        anchor.wait_for(state="visible", timeout=secs * 1000)
        page.wait_for_timeout(800)
        return True
    except PWTimeout:
        return False


def select_all(page):
    """Tick the grey header box next to 'Product Details'. Confirmed via 'xx/yy Orders Selected'."""
    sel, tot = selection_info(page)
    if sel is not None and tot and sel >= tot:
        return True                                   # already all selected
    if not wait_for_table(page):
        dismiss_popups(page)
        if not wait_for_table(page, 10):
            snap(page, "no_select_all")
            log.warning("Orders table ('%s' header) never appeared.", T["table_header_anchor"])
            return False
    for attempt in range(3):
        dismiss_popups(page)
        # a) the box sitting just left of "Product Details"
        pos = page.evaluate(HEADER_BOX_JS, T["table_header_anchor"])
        if pos:
            page.mouse.click(pos["x"], pos["y"])
            page.wait_for_timeout(1200)
            if selection_info(page)[0]:
                return True
        # b) a real checkbox element in that header row
        box = header_checkbox(page)
        if box is not None:
            try:
                if box.get_attribute("type") == "checkbox":
                    box.check(force=True, timeout=4000)
                else:
                    box.click(force=True, timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            if selection_info(page)[0]:
                return True
        page.wait_for_timeout(1500)
    snap(page, "no_select_all")
    return False


def selection_info(page):
    """Reads the footer text '20/20 Orders Selected' → (20, 20). (None, None) if not shown."""
    try:
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*Orders?\s+Selected", page.inner_text("body", timeout=5000), re.I)
        return (int(m.group(1)), int(m.group(2))) if m else (None, None)
    except Exception:
        return (None, None)


def selected_count(page):
    sel, tot = selection_info(page)
    if sel is not None:
        return f"{sel}/{tot}"
    return max(0, page.locator("input[type=checkbox]:checked, [role=checkbox][aria-checked=true]").count() - 1)


def click_first_button(page, labels, timeout=8000):
    """Try button labels in priority order (exact match first, e.g. 'Accept All' before 'Accept')."""
    for lab in labels:
        loc = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(lab)}\s*$", re.I))
        for k in range(loc.count()):
            el = loc.nth(k)
            if el.is_visible() and el.is_enabled():
                safe_click(page, el)
                return lab
    page.wait_for_timeout(min(timeout, 3000))
    for lab in labels:   # second pass after a short wait
        loc = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(lab)}\s*$", re.I))
        for k in range(loc.count()):
            el = loc.nth(k)
            if el.is_visible() and el.is_enabled():
                safe_click(page, el)
                return lab
    return None


def confirm_in_dialog(page, wait_ms=3000):
    """Click 'Accept Order' / Confirm / Yes only inside a pop-up — never on the orders table.
    Polls for up to `wait_ms` for the pop-up to appear."""
    waited = 0
    while True:
        if _confirm_once(page):
            return True
        if waited >= wait_ms:
            return False
        page.wait_for_timeout(500)
        waited += 500


def _confirm_once(page):
    dialogs = page.locator(CFG["selectors"]["popup"])
    for i in range(dialogs.count()):
        d = dialogs.nth(i)
        try:
            if not d.is_visible():
                continue
        except Exception:
            continue
        for lab in T["confirm_button"]:
            b = d.get_by_role("button", name=re.compile(rf"^\s*{re.escape(lab)}\s*$", re.I)).first
            if b.count() and b.is_visible() and b.is_enabled():
                b.click()
                log.info("Confirmed pop-up ('%s').", lab)
                return True
    return False


def wait_moved_to_rts(page, rts_before, expected, limit=None):
    """After accepting: open the Ready to Ship tab and keep re-checking until all
    accepted orders have arrived there (Ready to Ship (xx) grows by `expected`),
    or Pending (xx) hits 0, or time runs out. Returns (pending_now, rts_now)."""
    limit = limit or CFG.get("accept_wait_secs", 300)
    slack = CFG.get("leftover_ok", 9)            # cancellations / fresh orders make counts drift
    target = (rts_before or 0) + expected
    waited, pend, rts, prev_rts = 0, None, None, None
    same, last_pair = 0, None
    while waited < limit:
        page.wait_for_timeout(10000)
        waited += 10
        confirm_in_dialog(page, wait_ms=0)      # in case the confirmation showed up late
        open_tab(page, T["rts_tab"])            # click Ready to Ship → refreshes both counts
        pend = tab_count(page, T["pending_tab"])
        rts = tab_count(page, T["rts_tab"])
        log.info("  Ready to Ship %s/%s target · Pending %s", rts, target, pend)
        if pend == 0 or (pend is not None and pend <= slack):
            return pend, rts
        if rts is not None and rts >= target - slack:
            return pend, rts
        if rts is not None and rts == prev_rts and rts > (rts_before or 0):
            return pend, rts                     # counts settled — good enough
        prev_rts = rts
        same = same + 1 if (pend, rts) == last_pair else 0
        last_pair = (pend, rts)
        if same >= 3:                            # nothing changing for ~30s — move on
            log.info("Counts steady; leftovers will be picked up next run.")
            return pend, rts
    log.warning("Timed out waiting for orders to reach Ready to Ship.")
    return pend, rts


def accept_pending(page, dry_run, final=False):
    go_to_orders(page)
    open_tab(page, T["pending_tab"])
    start = tab_count(page, T["pending_tab"])
    log.info("Pending (%s) · Ready to Ship (%s)", start, tab_count(page, T["rts_tab"]))
    snap(page, "pending_before")
    if not start:
        return 0
    accepted_total = 0
    n = start
    for batch in range(CFG.get("max_batches", 20)):
        if batch:
            open_tab(page, T["pending_tab"])     # back to Pending for the next page of orders
        if not select_all(page):
            snap(page, "no_select_all")
            raise RuntimeError("Select-all checkbox not found on Pending tab (see no_select_all.png)")
        sel, _ = selection_info(page)
        log.info("Selected %s pending order(s).", selected_count(page))
        if dry_run:
            log.info("[dry-run] NOT clicking Accept Selected Orders.")
            pause_to_show(page)
            return 0
        rts_before = tab_count(page, T["rts_tab"])
        lab = click_first_button(page, T["accept_button"])
        if not lab:
            snap(page, "no_accept_button")
            raise RuntimeError("'Accept Selected Orders' button not found (see no_accept_button.png)")
        log.info("Clicked '%s'.", lab)
        confirm_in_dialog(page, wait_ms=8000)   # "Accepting orders" pop-up → Accept Order
        pend, rts = wait_moved_to_rts(page, rts_before, sel or n, limit=60 if final else None)
        moved = (rts - (rts_before or 0)) if rts is not None else (n - (pend or 0))
        accepted_total += max(0, moved)
        if pend == 0:
            log.info("Pending is 0 — all orders moved to Ready to Ship.")
            break
        if pend is not None and pend <= (0 if final else CFG.get("leftover_ok", 9)):
            log.info("Pending (%s) left — fine, the next run will pick them up.", pend)
            break
        if pend is not None and pend >= n:
            snap(page, "pending_after_accept")
            log.info("Pending is %s (new orders came in / still moving) — next run will pick them up.", pend)
            break
        n = pend
    snap(page, "pending_after")
    return accepted_total


LABEL_POPUP_RX = re.compile(r"labels?\s+generated|ready\s+to\s+ship\s+labels", re.I)


def label_popup(page):
    """The 'Ready to Ship Labels — Labels generated successfully for N orders' pop-up, if open."""
    pops = page.locator(CFG["selectors"]["popup"]).filter(has_text=LABEL_POPUP_RX)
    for k in range(pops.count()):
        if pops.nth(k).is_visible():
            return pops.nth(k)
    return None


def label_popup_button(pop):
    b = pop.get_by_role("button", name=re.compile(r"^\s*(download\s+)?labels?\s*$", re.I))
    for k in range(b.count()):
        if b.nth(k).is_visible() and b.nth(k).is_enabled():
            return b.nth(k)
    return None


def _seen_file():
    return BASE / "state" / f"labels_seen_{ACCT}.json"


def label_request_id(pop):
    """'Requested on 05 Oct 2026, 11:42 PM' + order count → unique id for that label batch."""
    try:
        t = pop.inner_text(timeout=2000)
    except Exception:
        return None
    req = re.search(r"Requested on\s*([^\n]+)", t, re.I)
    cnt = re.search(r"for\s+(\d+)\s+orders?", t, re.I)
    if not req:
        return None
    return f"{req.group(1).strip()} | {cnt.group(1) if cnt else '?'}"


def label_seen(rid):
    try:
        return rid in json.loads(_seen_file().read_text())
    except Exception:
        return False


def mark_label_seen(rid):
    if not rid:
        return
    try:
        seen = json.loads(_seen_file().read_text())
    except Exception:
        seen = []
    seen = (seen + [rid])[-500:]
    _seen_file().write_text(json.dumps(seen))


def close_label_popup(page):
    """After the PDF is saved: close the labels pop-up (Escape, else click its dark background)."""
    if not label_popup(page):
        return
    page.keyboard.press("Escape")
    page.wait_for_timeout(700)
    if label_popup(page):
        ov = page.locator("[aria-label='Close modal']").first
        try:
            if ov.count() and ov.is_visible():
                ov.click(position={"x": 6, "y": 6}, timeout=3000)   # the overlay itself, nothing underneath
        except Exception:
            pass
        page.wait_for_timeout(700)
    if label_popup(page):
        log.warning("Labels pop-up is still open after download.")


def save_download(page, click_fn, prefix):
    """Click and capture the label PDF — either a file download or a PDF that opens in a new tab."""
    out_dir = BASE / CFG.get("download_dir", "downloads") / ACCT / dt.date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    got = {"dl": None, "page": None}
    ctx = page.context
    on_dl = lambda d: got.__setitem__("dl", d)
    on_pg = lambda p: got.__setitem__("page", p)
    page.on("download", on_dl)
    ctx.on("page", on_pg)
    try:
        click_fn()
        clicked_popup = False
        for sec in range(int(CFG.get("label_wait_secs", 120))):
            if got["dl"] or got["page"]:
                break
            # Meesho opens "Ready to Ship Labels" → wait for "Labels generated…" → click its Label button
            pop = label_popup(page)
            if pop is not None and not clicked_popup:
                if CFG["features"].get("download_manifest"):
                    mf = pop.get_by_text(re.compile(r"download manifest", re.I)).first
                    try:
                        if mf.count():
                            mf.click(timeout=2000)          # tick "Download Manifest" too
                    except Exception:
                        pass
                btn = label_popup_button(pop)
                got["rid"] = label_request_id(pop)
                if btn is not None:
                    try:
                        msg = pop.inner_text(timeout=2000).split("\n")
                        log.info("Labels pop-up: %s", " | ".join(m for m in msg if m.strip())[:120])
                    except Exception:
                        pass
                    btn.click(timeout=5000)
                    clicked_popup = True
            elif sec == 2:
                confirm_in_dialog(page, wait_ms=0)
            page.wait_for_timeout(1000)
    finally:
        page.remove_listener("download", on_dl)
        ctx.remove_listener("page", on_pg)
    if got["dl"]:
        d = got["dl"]
        path = out_dir / f"{prefix}_{RUN_ID}_{d.suggested_filename}"
        d.save_as(str(path))
    elif got["page"]:
        p = got["page"]
        p.wait_for_load_state(timeout=TIMEOUT)
        resp = ctx.request.get(p.url)
        path = out_dir / f"{prefix}_{RUN_ID}.pdf"
        path.write_bytes(resp.body())
        p.close()
    else:
        snap(page, f"{prefix}_no_file")
        log.warning("Clicked %s but no file arrived (see %s_no_file.png).", prefix, prefix)
        close_label_popup(page)
        return None
    log.info("Saved %s", path)
    mark_label_seen(got.get("rid"))
    close_label_popup(page)
    return path


def set_max_rows(page):
    """If the table has a 'Rows per page' / page-size selector, pick the biggest option."""
    try:
        sel = page.locator("select").filter(has=page.locator("option"))
        for k in range(sel.count()):
            opts = [o for o in sel.nth(k).locator("option").all_inner_texts() if o.strip().isdigit()]
            if opts:
                sel.nth(k).select_option(label=max(opts, key=int))
                settle(page)
                log.info("Rows per page set to %s.", max(opts, key=int))
                return
        rpp = page.get_by_text(re.compile(r"rows per page|items per page|per page", re.I)).first
        if rpp.count() and rpp.is_visible():
            box = page.locator("[aria-haspopup=listbox], [role=combobox]").last
            if box.count() and box.is_visible():
                safe_click(page, box)
                page.wait_for_timeout(600)
                opts = page.get_by_role("option")
                vals = [(opts.nth(i), opts.nth(i).inner_text().strip()) for i in range(opts.count())]
                vals = [(o, t) for o, t in vals if t.isdigit()]
                if vals:
                    o, t = max(vals, key=lambda x: int(x[1]))
                    safe_click(page, o)
                    settle(page)
                    log.info("Rows per page set to %s.", t)
                else:
                    page.keyboard.press("Escape")
    except Exception as e:
        log.info("No page-size option used (%s).", str(e)[:80])


def next_page(page):
    """Click the table's 'next page' control. Returns False when there is no next page."""
    cands = page.locator(
        "[aria-label*='next page' i], [aria-label='Next' i], [aria-label*='go to next' i], "
        "button[class*='next' i], li[class*='next' i] > *, [data-testid*='next' i]")
    for k in range(cands.count()):
        el = cands.nth(k)
        try:
            if el.is_visible():
                dis = el.get_attribute("disabled") is not None or el.get_attribute("aria-disabled") == "true" \
                      or "disabled" in (el.get_attribute("class") or "").lower()
                if dis:
                    return False
                safe_click(page, el)
                settle(page)
                log.info("Moved to next page.")
                return True
        except Exception:
            pass
    for txt in ("Next", "›", ">", "»"):
        b = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(txt)}\s*$", re.I)).first
        if b.count() and b.is_visible() and b.is_enabled():
            safe_click(page, b)
            settle(page)
            log.info("Moved to next page.")
            return True
    return False


def wait_until(page, when, label=""):
    """Sleep (keeping the page alive and pop-up free) until `when`."""
    last_pop = time.time()
    while True:
        left = (when - dt.datetime.now()).total_seconds()
        if left <= 0:
            return
        if left > 30 and time.time() - last_pop > 20:
            dismiss_popups(page)
            last_pop = time.time()
        if int(left) % 60 == 0 and left > 5:
            log.info("  waiting %s — %d s to go", label, int(left))
        page.wait_for_timeout(min(1000, max(50, int(left * 1000))))


def ready_to_ship(page, dry_run, at=None):
    if not CFG["features"].get("download_labels"):
        return 0, []
    go_to_orders(page)
    open_tab(page, T["rts_tab"])
    dismiss_popups(page)
    files = []

    # 1) A "Ready to Ship Labels — Labels generated…" pop-up already open → save it once only
    page.wait_for_timeout(1500)
    pop = label_popup(page)
    if pop is not None:
        rid = label_request_id(pop)
        btn = label_popup_button(pop)
        if rid and label_seen(rid):
            log.info("Labels pop-up for '%s' was already downloaded earlier — closing it.", rid)
            close_label_popup(page)
        elif btn is not None:
            log.info("Earlier labels ready (%s) — downloading.", rid)
            f = save_download(page, lambda: btn.click(timeout=5000), "past_labels")
            mark_label_seen(rid)
            files += [f] if f else []
        settle(page)
    dismiss_popups(page)

    # 2) select all on Ready to Ship → bottom-right Label button, page by page
    n = tab_count(page, T["rts_tab"])
    log.info("Ready to Ship (%s)", n)
    if n is None:                                # couldn't read the number — carry on anyway
        tabs = page.get_by_role("tab").all_inner_texts()
        log.warning("Couldn't read the Ready to Ship count. Tabs seen: %s", [t.strip() for t in tabs][:8])
        snap(page, "rts_count_unknown")
        n = 10**6
    if n == 0:
        return 0, files
    set_max_rows(page)
    if at is not None:
        # get everything ready, then click Label at the exact time
        wait_until(page, at - dt.timedelta(seconds=CFG.get("reselect_before_secs", 30)), "to re-select")
        open_tab(page, T["rts_tab"])              # refresh: include orders accepted in the last minutes
        dismiss_popups(page)
    for pg in range(1, CFG.get("max_pages", 30) + 1):
        if not select_all(page):
            snap(page, "rts_no_select_all")
            raise RuntimeError("Select-all checkbox not found on Ready to Ship tab")
        log.info("Page %d: selected %s ready-to-ship order(s).", pg, selected_count(page))
        pause_to_show(page)
        sel, tot = selection_info(page)
        if at is not None and pg == 1:
            click_got_it(page)
            wait_until(page, at, "for label time")
            log.info("⏰ %s — clicking Label now.", dt.datetime.now().strftime("%H:%M:%S.%f")[:-3])
        f = save_download(page, lambda: click_first_button(page, T["label_button"]), f"labels_p{pg}")
        files += [f] if f else []
        settle(page)
        if sel is not None and sel >= tot:      # e.g. "220/220 Orders Selected" → all done
            break
        now = tab_count(page, T["rts_tab"])
        if now is not None and now < n:      # orders left the tab after labelling → same page again
            log.info("Ready to Ship dropped %s → %s; staying on this page.", n, now)
            n = now
            if now == 0:
                break
            continue
        if not next_page(page):
            break
    if CFG["features"].get("download_manifest"):
        settle(page)
        select_all(page)
        f = save_download(page, lambda: click_first_button(page, T["manifest_button"]), "manifest")
        files += [f] if f else []
    snap(page, "rts_after")
    return n, files


# ---------------------------------------------------------------- main
def label_time(hhmm):
    """'00:00' → the next 00:00 (today or tomorrow). If it was <5 min ago, use it (click now)."""
    h, m = map(int, hhmm.split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t < now - dt.timedelta(minutes=5):
        t += dt.timedelta(days=1)
    return t


def run(args):
    with sync_playwright() as p:
        launch = {"headless": not args.headed}
        if os.environ.get("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = p.chromium.launch(**launch)
        ctx_kwargs = {
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
        }
        if STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(STATE_FILE)
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        try:
            ensure_logged_in(page, context)
            if args.login_only:
                return "Login OK, session saved."
            at = label_time(args.label_at) if args.label_at else None
            accepted = accept_pending(page, args.dry_run) if CFG["features"].get("accept_pending") else 0
            if at is not None and CFG["features"].get("accept_pending"):
                # one last accept pass ~2 min before label time, so late orders are included
                lead = CFG.get("final_accept_before_secs", 150)
                wait_until(page, at - dt.timedelta(seconds=lead), "for final accept")
                log.info("Final accept pass before %s.", at.strftime("%H:%M"))
                accepted += accept_pending(page, args.dry_run, final=True)
            rts, files = ready_to_ship(page, args.dry_run, at=at)
            context.storage_state(path=str(STATE_FILE))  # refresh session
            mode = " (DRY RUN)" if args.dry_run else ""
            return (f"[{ACCT}]{mode} accepted {accepted} order(s); "
                    f"{rts} ready-to-ship; {len(files)} file(s) downloaded.")
        except Exception:
            snap(page, "error")
            raise
        finally:
            context.close()
            browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--login-only", action="store_true")
    ap.add_argument("--account", help="run only this account name (e.g. ruchika)")
    ap.add_argument("--sequential", action="store_true", help="run accounts one after another instead of in parallel")
    ap.add_argument("--label-at", help="click Label at exactly this time, e.g. 00:00 (start the run ~10 min earlier)")
    args = ap.parse_args()

    global HEADED
    HEADED = args.headed
    setup_logging()
    cleanup_old_runs()
    accounts = load_accounts()
    if not accounts:
        log.error("No accounts found in .env (ACCOUNT_1_LOGIN, ACCOUNT_1_PASSWORD …)")
        return 1
    if args.account:
        accounts = [a for a in accounts if a["name"] == args.account]

    # Several accounts → run them side by side (each in its own browser), unless --sequential
    if len(accounts) > 1 and not args.sequential:
        log.info("Starting %d accounts in parallel%s.", len(accounts),
                 f" — Label at {args.label_at}" if args.label_at else "")
        base = [sys.executable, str(BASE / "bot.py")]
        base += ["--label-at", args.label_at] if args.label_at else []
        base += ["--login-only"] if args.login_only else []
        base += ["--dry-run"] if args.dry_run else []
        base += ["--headed"] if args.headed else []
        procs = [subprocess.Popen(base + ["--account", a["name"]]) for a in accounts]
        codes = [pr.wait() for pr in procs]
        return 1 if any(codes) else 0

    lines, failed = [], 0
    for a in accounts:            # one account at a time, fully isolated sessions
        use_account(a)
        lock_path = BASE / "state" / f"bot_{ACCT}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(lock_path, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("[%s] another run is busy with this account — skipping.", ACCT)
            continue
        global HOME_URL, ORDERS_URL
        HOME_URL = ORDERS_URL = ""
        log.info("===== %s =====", ACCT)
        attempts = CFG.get("retries", 1) + 1
        last_err = None
        for i in range(attempts):
            try:
                lines.append(run(args))
                last_err = None
                break
            except Exception as e:
                last_err = e
                log.error("[%s] attempt %d failed: %s\n%s", ACCT, i + 1, e, traceback.format_exc())
                if i == 0 and "login" in str(e).lower() and STATE_FILE.exists():
                    STATE_FILE.unlink()  # login problem → fresh login on retry
                if i + 1 < attempts:
                    log.info("[%s] retrying (keeping the saved login)…", ACCT)
        lock.close()
        if last_err:
            failed += 1
            lines.append(f":warning: [{ACCT}] FAILED: {last_err} (see runs/{RUN_ID}/{ACCT}/)")

    if lines:
        notify(f"Meesho bot run {RUN_ID}\n" + "\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
