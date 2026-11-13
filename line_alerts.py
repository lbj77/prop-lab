"""
Pinnacle move alerts -> Discord.

Pinnacle doesn't publish news, but it reacts to news faster than anyone. So this
watches Pinnacle's player-prop lines between refreshes and pings Discord when:
  * a line moves 1+ point (or 2+ for big stats like PRA / yards), or the price
    swings 8+ points of fair chance at the same line
  * Pinnacle PULLS a player's props (often the first public sign of an injury)
  * several players on one team move together (something is going on there)
Each alert lists the pick'em / soft books that still have the OLD line, which is
your window before they catch up.

Run it right after a lines refresh (refresh_injuries.bat does this):
    python line_alerts.py                 # NBA (props_data.json)
    python line_alerts.py nfl             # NFL (nfl_props_data.json)

One-time setup: make a Discord channel (e.g. #line-alerts), Edit Channel ->
Integrations -> Webhooks -> New Webhook -> Copy URL, then in Command Prompt:
    setx DISCORD_WEBHOOK_ALERTS "https://discord.com/api/webhooks/..."
(close and reopen Command Prompt afterwards). Turn on phone notifications for
that channel in the Discord app.
"""
import json
import os
import sys
from datetime import date, datetime, timezone

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SPORT = (sys.argv[1] if len(sys.argv) > 1 else "nba").lower()
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "props_data.json" if SPORT == "nba" else "nfl_props_data.json")
SNAP = os.path.join(HERE, f"pinnacle_snapshot_{SPORT}.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ALERTS")
_hook_file = os.path.join(HERE, "alerts_webhook.txt")
if not WEBHOOK and os.path.exists(_hook_file):
    with open(_hook_file, encoding="utf-8") as _f:
        WEBHOOK = _f.read().strip() or None
WATCH = {"prizepicks": "PrizePicks", "pick6": "Pick6", "sleeper": "Sleeper", "underdog": "Underdog",
         "betr_us_dfs": "Betr", "bovada": "Bovada", "draftkings": "DraftKings", "fanduel": "FanDuel",
         "prophetx": "ProphetX", "kalshi": "Kalshi"}
BIG_STATS = {"PRA", "PR", "PA", "Pass Yds", "Rush Yds", "Rec Yds", "Rush+Rec Yds"}


def dec(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def fair_over(o, u):
    po, pu = 1 / dec(o), 1 / dec(u)
    return po / (po + pu)


def mins_old(ts):
    """How many minutes old a book's price was when we pulled it (None if the feed didn't say)."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 60))
    except Exception:
        return None


def snapshot(data):
    cur = {}
    for p in data.get("players", []):
        nxt = p.get("next") or {}
        for k, L in (p.get("lines") or {}).items():
            books = L.get("books") or {}
            # sharpest source available: Pinnacle, then the US exchanges (Novig, ProphetX)
            src, pin = next(((b, books[b]) for b in ("pinnacle", "novig", "prophetx")
                             if books.get(b) and books[b].get("over") not in (None, "") and books[b].get("under") not in (None, "")), (None, None))
            if not pin:
                continue
            others, ages = {}, {}
            for b, v in books.items():
                base = b.split("~")[0]
                if base in WATCH and "~" not in b and base != src:
                    others[WATCH[base]] = [v.get("line"), v.get("s", "ou")]   # line, and which sides the app offers
                    if v.get("t"):
                        ages[WATCH[base]] = v["t"]
            cur[f"{p['name']}|{k}"] = {"name": p["name"], "stat": k, "team": p.get("team", ""),
                                       "opp": nxt.get("opp", ""), "line": float(pin["line"]),
                                       "fair": round(fair_over(pin["over"], pin["under"]), 4), "others": others, "ages": ages, "src": src}
    return cur


# ---------- Prop Lab context: what the app's Strategy lab says about each alerted prop ----------
LAB_NAMES = {"l5": "Perfect last 5", "l10": "Hit 8+ of last 10", "l15": "70%+ of last 15", "l30": "70%+ of last 30",
             "szn": "65%+ full log", "h2h": "Head to head", "def": "Soft defense", "edge": "Model edge 5%+",
             "score": "Hit score 75+", "boost": "Teammate out", "l10def": "Last 10 + soft defense", "scedge": "Score 75+ and edge 5%+"}


BASE = ["l5", "l10", "l15", "l30", "szn", "h2h", "def", "edge", "score", "boost"]   # same order as the app


def strat_name(i):
    if i.startswith("combo:"):
        return " + ".join(LAB_NAMES.get(x, x) for x in i[6:].split("+"))
    if i.startswith("conf:"):
        return f"{i[5:]}+ strategies agree"
    return LAB_NAMES.get(i, i)


def best_fit(L, side, recs, user_strat=None):
    """The best proven strategy or combo this alert fits (same rules as the app). None if nothing proven."""
    if not L or L["side"] != side:
        return None
    fl = list(L["flags"]); base = [b for b in BASE if b in fl]
    cands = [(f, recs[f], False) for f in fl if f in recs]
    for i1 in range(len(base)):
        for i2 in range(i1 + 1, len(base)):
            for cid in [f"combo:{base[i1]}+{base[i2]}"] + [f"combo:{base[i1]}+{base[i2]}+{base[i3]}" for i3 in range(i2 + 1, len(base))]:
                if cid in recs:
                    cands.append((cid, recs[cid], True))
    for k in range(2, min(len(base), 4) + 1):
        if f"conf:{k}" in recs:
            cands.append((f"conf:{k}", recs[f"conf:{k}"], True))
    ids = [c[0] for c in cands]
    if user_strat and user_strat != "all":
        ok = user_strat in fl or (user_strat.startswith("combo:") and all(x in fl for x in user_strat[6:].split("+"))) \
            or (user_strat.startswith("conf:") and len(base) >= int(user_strat[5:]))
        return {"id": user_strat, "rec": recs.get(user_strat, [0, 0, 0.0]), "ids": ids + [user_strat]} if ok else None

    def proven(c):
        w, n, pr = c[1]; roi = pr / n if n else 0
        return (n >= 100 and roi >= .05) if c[2] else (n >= 300 and roi > .02)
    pv = [c for c in cands if proven(c)]
    if not pv:
        return None
    best = max(pv, key=lambda c: (c[1][2] / c[1][1]) * c[1][1] / (c[1][1] + 200))
    return {"id": best[0], "rec": best[1], "ids": ids}


def push_phones(items, ctx):
    """Lock-screen notifications to every device that turned on phone alerts in Prop Lab,
    only for proven alerts that pass that device's filters."""
    if not items:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, messaging
        key = os.path.join(HERE, "firebase-key.json")
        if not os.path.exists(key):
            return
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(key))
        db = firestore.client()
        subs = [d for d in db.collection_group("push").stream() if f"sports/{SPORT}/" in d.reference.path]
    except Exception as e:
        print(f"(Couldn't read phone-alert devices: {str(e)[:120]})")
        return
    if not subs:
        return
    recs = (ctx or {}).get("recs", {})
    leans = (ctx or {}).get("leans", {})
    sent = bad = 0
    for d in subs:
        sub = d.to_dict() or {}
        tok = sub.get("token")
        if not tok:
            continue
        F = sub.get("filters") or {}
        fs, fside, fstat = F.get("strat", "any"), F.get("side", "both"), F.get("stat", "all")
        fmin, fmine = float(F.get("minWin") or 0), bool(F.get("mine"))
        bname = (sub.get("bookName") or "").lower()
        page = sub.get("url") or ""
        base_url = page.rsplit("/", 1)[0] + "/" if page.startswith("https://") else ""
        msgs = []
        for a in items:
            if a.get("kind") == "pulled":
                if fs != "any" or fmin > 0:
                    continue
                title = f"\U0001F6A8 {SPORT.upper()}: {a.get('pick', '')}"
                body = a.get("note", "")
            elif a.get("kind") == "move":
                fit = best_fit(leans.get((a.get("name"), a.get("stat"))), a.get("side"), recs, sub.get("strat"))
                if not fit:
                    continue
                w, n, pr = fit["rec"]
                if fside != "both" and a.get("side") != fside:
                    continue
                if fstat != "all" and a.get("stat") != fstat:
                    continue
                if fs != "any" and fs not in fit["ids"]:
                    continue
                if fmin > 0 and not (n and w / n * 100 >= fmin):
                    continue
                if fmine and bname and not any(bname in str(x).lower() for x in a.get("where", [])):
                    continue
                if not a.get("where"):
                    continue
                title = f"\U0001F7E2 {SPORT.upper()} BET IT: {a.get('pick', '')}"
                body = (f"{strat_name(fit['id'])}: won {w / n:.0%} of {n}. " if n else f"{strat_name(fit['id'])}. ") + \
                       f"Where: {', '.join(a['where'][:4])}. {a.get('why', '')}"
            else:
                continue
            msgs.append((title, body))
        if not msgs:
            continue
        if len(msgs) > 4:
            msgs = msgs[:3] + [(f"\U0001F4E3 {SPORT.upper()}: {len(msgs) - 3} more proven alerts", "Open the Alerts tab in Prop Lab to see them all.")]
        out = []
        for i, (t, b) in enumerate(msgs):
            out.append(messaging.Message(token=tok, data={"title": t, "body": b, "url": base_url + f"{SPORT}.html#alerts"},
                webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "3600"},
                    notification=messaging.WebpushNotification(title=t, body=b[:230], icon=base_url + "icon-192.png" if base_url else None,
                                                              tag=f"pl-{SPORT}-{datetime.now().strftime('%H%M')}-{i}"),
                    fcm_options=messaging.WebpushFCMOptions(link=base_url + f"{SPORT}.html#alerts") if base_url else None)))
        try:
            res = messaging.send_each(out)
            sent += res.success_count
            for r in res.responses:
                if not r.success and r.exception is not None and type(r.exception).__name__ in ("UnregisteredError", "SenderIdMismatchError"):
                    d.reference.delete(); bad += 1
                    break
                if not r.success:
                    print(f"(Phone alert failed: {str(r.exception)[:120]})")
        except Exception as e:
            print(f"(Couldn't send phone alerts: {str(e)[:120]})")
    print(f"Sent {sent} phone notification(s) to {len(subs)} device(s)." + (f" Removed {bad} device(s) that turned alerts off." if bad else ""))


def lab_context():
    """Reads the Strategy lab from your Prop Lab web app (Firebase). Returns today's leans and the track records."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        key = os.path.join(HERE, "firebase-key.json")
        if not os.path.exists(key):
            return None
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(key))
        db = firestore.client()
        docs = [d.to_dict() for d in db.collection("sports").document(SPORT).collection("public")
                .document("labmeta").collection("days").stream()]
    except Exception as e:
        print(f"(Couldn't read the Strategy lab: {str(e)[:120]})")
        return None
    today = date.today().isoformat()
    leans, strat, grade, sg, recs = {}, {}, {}, {}, {}

    def addrec(k, win, pl):
        r = recs.setdefault(k, [0, 0, 0.0]); r[0] += win; r[1] += 1; r[2] += pl
    for d in sorted([d for d in docs if d and d.get("rows")], key=lambda d: d.get("date", "")):
        res = d.get("res") or ""
        for i, row in enumerate(d["rows"]):
            f = row.split("|")
            if len(f) < 7:
                continue
            r = res[i] if i < len(res) else "."
            if r in ("h", "m"):
                win = r == "h"
                try:
                    o = float(f[5]); pl = (o / 100 if o > 0 else 100 / -o) if win else -1.0
                except Exception:
                    pl = 0.87 if win else -1.0
                fl_all = [x for x in f[6].split(",") if x]
                for fl in fl_all:
                    if fl != "all":
                        addrec(fl, win, pl)
                on = [b for b in BASE if b in fl_all]
                for i1 in range(len(on)):
                    for i2 in range(i1 + 1, len(on)):
                        addrec(f"combo:{on[i1]}+{on[i2]}", win, pl)
                        for i3 in range(i2 + 1, len(on)):
                            addrec(f"combo:{on[i1]}+{on[i2]}+{on[i3]}", win, pl)
                for k in range(2, min(len(on), 4) + 1):
                    addrec(f"conf:{k}", win, pl)
                g = f[8] if len(f) > 8 else ""
                for fl in f[6].split(","):
                    w, n = strat.get(fl, (0, 0)); strat[fl] = (w + (r == "h"), n + 1)
                    if g:
                        w, n = sg.get((fl, g), (0, 0)); sg[(fl, g)] = (w + (r == "h"), n + 1)
                if g in ("A", "B", "C", "D"):
                    w, n = grade.get(g, (0, 0)); grade[g] = (w + (r == "h"), n + 1)
            if d.get("date", "") >= today:          # today's (or the next game day's) recorded leans
                leans.setdefault((f[0], f[2]), {"side": "over" if f[3] == "o" else "under", "line": f[4],
                                                "flags": [x for x in f[6].split(",") if x and x != "all"],
                                                "prob": float(f[7]) if len(f) > 7 and f[7] else None,
                                                "grade": f[8] if len(f) > 8 else ""})
    return {"leans": leans, "strat": strat, "grade": grade, "sg": sg, "recs": recs}


def push_app(items):
    """Shows the alerts inside the Prop Lab app too (Today tab). Only the last hour is kept, so it stays fresh."""
    if not items:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        key = os.path.join(HERE, "firebase-key.json")
        if not os.path.exists(key):
            return
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(key))
        ref = firestore.client().collection("sports").document(SPORT).collection("public").document("alerts")
        now = datetime.now(timezone.utc)
        old = []
        snap = ref.get()
        if snap.exists:
            old = [a for a in (snap.to_dict() or {}).get("items", [])
                   if a.get("t") and (now - datetime.fromisoformat(a["t"].replace("Z", "+00:00"))).total_seconds() < 3600]
        ref.set({"items": (items + old)[:80], "updatedAt": now.isoformat()})
        print(f"Sent {len(items)} alert(s) to the app.")
    except Exception as e:
        print(f"(Couldn't send alerts to the app: {str(e)[:120]})")


def assess(m, side, ctx):
    """Combine the sharp move with Prop Lab's Strategy lab into one plain verdict."""
    pin = m.get("src") == "pinnacle"
    L = ctx["leans"].get((m["name"], m["stat"])) if ctx else None
    info = {"agree": None, "grade": "", "fit": None}
    if L:
        info["agree"] = L["side"] == side
        info["grade"] = L["grade"]
        # the best-performing strategy this prop fits (30+ graded picks), with its record at this grade if known
        best = None
        for fl in L["flags"]:
            w, n = ctx["strat"].get(fl, (0, 0))
            if n >= 30 and (best is None or w / n > best[1]):
                best = (fl, w / n, n)
        if best:
            gw, gn = ctx["sg"].get((best[0], L["grade"]), (0, 0))
            info["fit"] = (LAB_NAMES.get(best[0], best[0]), best[1], best[2], (gw / gn if gn >= 15 else None), gn)
    fit_rate = info["fit"][3] if info["fit"] and info["fit"][3] is not None else (info["fit"][1] if info["fit"] else None)
    if info["agree"] is False:
        verdict = ("\U0001F7E1", "SMALL BET", "Pinnacle beats our model, but they disagree") if pin else \
                  ("\U0001F534", "SKIP", "our model disagrees and the signal isn't Pinnacle")
    elif info["agree"] and fit_rate is not None and fit_rate >= .57:
        verdict = ("\U0001F7E2", "TAKE IT", "sharp move + Prop Lab agrees + a winning strategy")
    elif info["agree"] and pin:
        verdict = ("\U0001F7E2", "TAKE IT", "Pinnacle moved and Prop Lab agrees")
    elif info["agree"]:
        verdict = ("\U0001F7E1", "SMALL BET", "Prop Lab agrees, but no strong strategy behind it")
    else:
        verdict = ("\U0001F7E2" if pin else "\U0001F7E1", "TAKE IT" if pin else "SMALL BET",
                   "Pinnacle moved (not in today's Prop Lab yet)" if pin else "sharp move only (not in today's Prop Lab yet)")
    return verdict, info


def main():
    if not os.path.exists(DATA):
        sys.exit(f"Missing {DATA}")
    with open(DATA, encoding="utf-8") as f:
        cur = snapshot(json.load(f))
    state = {}
    if os.path.exists(SNAP):
        with open(SNAP, encoding="utf-8") as f:
            state = json.load(f)
    now = datetime.now(timezone.utc)
    fresh_day = not state.get("time") or (now - datetime.fromisoformat(state["time"])).total_seconds() > 12 * 3600 \
        or datetime.fromisoformat(state["time"]).astimezone().date() != now.astimezone().date()
    base = {} if fresh_day else state.get("base", {})       # line at the start of the day or at the last alert
    last = {} if fresh_day else state.get("lines", {})      # previous refresh (for pulled props)
    prev_time = state.get("time")
    pending = {} if fresh_day else state.get("pending", {})
    for key, c in cur.items():
        # only ever compare a book with itself: if today's sharp source changed (Novig -> ProphetX), start over quietly
        if key not in base or base[key].get("src") != c.get("src"):
            base[key] = c
            pending.pop(key, None)
    if fresh_day:
        with open(SNAP, "w", encoding="utf-8") as f:
            json.dump({"time": now.isoformat(), "lines": cur, "base": base, "pending": pending}, f)
        print(f"{SPORT.upper()}: start-of-day baseline saved ({len(cur)} sharp lines). Alerts start from the next refresh.")
        if WEBHOOK and cur:   # one short "I'm on" message per sport per day, so you know it's working
            try:
                requests.post(WEBHOOK, json={"content": f"\U0001F7E2 {SPORT.upper()} line alerts are on today: watching {len(cur)} sharp lines every 10 minutes."}, timeout=20)
            except Exception:
                pass
        return
    prev = base
    moves, pulled = [], {}
    for key, c in cur.items():
        p = prev.get(key)
        if not p:
            continue
        dl = c["line"] - p["line"]
        need = 2.0 if c["stat"] in BIG_STATS else 1.0
        df = c["fair"] - p["fair"]
        if abs(dl) >= need or (dl == 0 and abs(df) >= 0.08):
            # Pinnacle moves are trusted right away. Exchange moves (Novig, ProphetX) flicker as orders come and go,
            # so they must still be there on the next check (10 min later) before they're sent.
            if c.get("src") != "pinnacle":
                pd = pending.get(key)
                if not pd or pd.get("line") != c["line"] or abs(pd.get("fair", 0) - c["fair"]) > .05:
                    pending[key] = {"line": c["line"], "fair": c["fair"]}
                    continue
            pending.pop(key, None)
            need_side = "u" if (dl < 0 or (dl == 0 and df < 0)) else "o"
            stale = []
            for b, info in c["others"].items():
                ln, sides = (info if isinstance(info, list) else [info, "ou"])
                if ln is None or float(ln) != p["line"] or need_side not in (sides or "ou"):
                    continue          # moved already, or the app no longer offers that side (e.g. More only)
                age = mins_old(c.get("ages", {}).get(b))
                stale.append(b if age is None else f"{b} ({age} min old data)" if age >= 15 else b)
            moves.append({**c, "from": p["line"], "dl": dl, "df": df, "stale": stale})
        else:
            pending.pop(key, None)   # moved back: it was noise
    # props Pinnacle had before but took down, grouped by player
    cur_players = {v["name"] for v in cur.values()}
    for key, p in last.items():
        if key not in cur and p["name"] not in cur_players:
            pulled.setdefault(p["name"], p)
    # alerted lines become the new base, so the same move isn't sent twice
    for m in moves:
        base[f"{m['name']}|{m['stat']}"] = cur[f"{m['name']}|{m['stat']}"]
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump({"time": now.isoformat(), "lines": cur, "base": base, "pending": pending}, f)

    if not moves and not pulled:
        print(f"{SPORT.upper()}: no big sharp-line moves since the start of the day (checked {len(cur)} lines).")
        return

    NAMES = {"PTS": "points", "REB": "rebounds", "AST": "assists", "3PM": "threes", "PRA": "pts+reb+ast", "PR": "pts+reb",
             "PA": "pts+ast", "RA": "reb+ast", "BLK": "blocks", "STL": "steals", "TOV": "turnovers",
             "Pass Yds": "passing yards", "Rush Yds": "rushing yards", "Rec Yds": "receiving yards", "Rec": "receptions",
             "Pass TD": "passing TDs", "Comp": "completions", "Att": "pass attempts", "INT": "interceptions", "TD": "anytime TD",
             "Rush+Rec Yds": "rush+rec yards", "Pass Att": "pass attempts"}
    nice = lambda k: NAMES.get(k, k)
    when = datetime.now().strftime('%I:%M %p').lstrip('0')
    lines = [f"\U0001F4E3 **Sharp money moves: {SPORT.upper()}, {when}**",
             "_Check the app under Where still shows that exact number before betting. Slow apps fix it within minutes._"]
    for name, p in list(pulled.items())[:5]:
        lines.append(f"\n\U0001F6A8 **{name} ({p['team']}): props pulled.** Likely injury news.\n"
                     f"   Skip his props. Look at his teammates instead: Prop Lab > Finder > Odds board > set him Out.")
    by_team = {}
    for m in moves:
        by_team.setdefault(m["team"], set()).add(m["name"])
    for team, names in by_team.items():
        if len(names) >= 3:
            lines.append(f"\n\u26A0\uFE0F **{team}: {len(names)} players moved at once.** Something may be going on with {team}. Check the news.")

    def item(m):
        stat = nice(m["stat"])
        down = m["dl"] < 0 or (m["dl"] == 0 and m["df"] < 0)
        side = "under" if down else "over"
        src = "Pinnacle (strong)" if m.get("src") == "pinnacle" else ("Novig" if m.get("src") == "novig" else "ProphetX")
        if m["stat"] == "TD":
            pick = f"{m['name']} anytime TD: {'NO' if down else 'YES'}"
            move = f"sharp chance he scores {m['fair'] - m['df']:.0%} \u2192 {m['fair']:.0%}"
            v, info = (("\U0001F534", "SKIP", "sharp books cooled on him") if down else ("\U0001F7E1", "SMALL BET", "sharp books like him to score")), {}
        else:
            num = m["from"] if m["dl"] != 0 else m["line"]
            pick = f"{m['name']} {side.upper()} {num:g} {stat}"
            move = (f"sharp line {m['from']:g} \u2192 {m['line']:g}" if m["dl"] != 0 else
                    f"sharp chance of the over {m['fair'] - m['df']:.0%} \u2192 {m['fair']:.0%}")
            v, info = assess(m, side, ctx)
        app = {"t": stamp, "kind": "move", "tone": {"TAKE IT": "g", "SMALL BET": "y"}.get(v[1], "r"), "verdict": v[1],
               "pick": pick, "name": m["name"], "stat": m["stat"], "team": m["team"], "side": side,
               "line": (m["from"] if m["dl"] != 0 else m["line"]) if m["stat"] != "TD" else None, "src": m.get("src"), "why": f"{move} on {src}", "note": v[2],
               "where": [] if (m["stat"] == "TD" and down) else list(m["stale"])}
        lines_ = [f"{v[0]} **{v[1]}: {pick}** ({m['team']})"]
        if m["stale"] and not (m["stat"] == "TD" and down):
            lines_.append(f"   \U0001F4CD Where: {', '.join(m['stale'])}")
        lines_.append(f"   \U0001F4C8 Why: {move} on {src}")
        if info.get("agree") is not None:
            lab = ("agrees" if info["agree"] else "leans the other way")
            g = f", grade {info['grade']}" if info.get("grade") else ""
            lines_.append(f"   \U0001F9EA Prop Lab {lab}{g}")
            app["lab"] = f"Prop Lab {lab}{g}"
        if info.get("fit"):
            nm, rate, n, grate, gn = info["fit"]
            gtxt = f"; grade {info['grade']} in it: {grate:.0%} of {gn}" if grate is not None else ""
            if info.get("agree") is False:
                lines_.append(f"   \U0001F3C6 Strategy backing the {'OVER' if side == 'under' else 'UNDER'} instead: {nm}, {rate:.0%} of {n}{gtxt}")
            else:
                lines_.append(f"   \U0001F3C6 Best strategy it fits: {nm}, {rate:.0%} of {n}{gtxt}")
            app["strat"] = f"{nm}: won {rate:.0%} of {n}"
        lines_.append(f"   \u27A1\uFE0F {v[2]}")
        return "\n".join(lines_), v[1], app

    stamp = datetime.now(timezone.utc).isoformat()
    app_items = [{"t": stamp, "kind": "pulled", "tone": "r", "name": name, "team": p["team"],
                  "pick": f"{name} ({p['team']}): props pulled", "note": "Likely injury news. Skip his props and look at his teammates (Injuries tab)."}
                 for name, p in list(pulled.items())[:5]]
    ctx = lab_context() if moves else None
    act = [m for m in moves if m["stale"]]
    fyi = [m for m in moves if not m["stale"]]
    order = lambda m: (m.get("src") != "pinnacle", -len(m["stale"]), -abs(m["dl"]) - abs(m["df"]))
    if act:
        lines.append("\n**Old number still out there:**  \U0001F7E2 take it   \U0001F7E1 small bet   \U0001F534 skip")
        rank = {"TAKE IT": 0, "SMALL BET": 1, "SKIP": 2}
        built = sorted([item(m) for m in sorted(act, key=order)], key=lambda t: rank.get(t[1], 3))
        for i, (text, _, a) in enumerate(built):
            if i < 8:
                lines.append("\n" + text)
            app_items.append(a)
    for m in sorted(fyi, key=order):   # already moved everywhere: the app shows these under "Other line moves"
        try:
            _, _, a = item(m)
            a["kind"] = "moved"; a["where"] = []
            app_items.append(a)
        except Exception:
            pass
    if fyi:
        lines.append("\n**\U0001F440 Already moved everywhere (just so you know):**")
        lines.append(", ".join(f"{m['name']} {nice(m['stat'])} {'\u2193' if (m['dl'] < 0 or (m['dl'] == 0 and m['df'] < 0)) else '\u2191'}" for m in sorted(fyi, key=order)[:10]))
    push_app(app_items)
    push_phones(app_items, ctx)
    msg = "\n".join(lines)[:1900]
    sent = "No DISCORD_WEBHOOK_ALERTS set (or alerts_webhook.txt), so nothing was sent."
    if WEBHOOK:
        try:
            r = requests.post(WEBHOOK, json={"content": msg}, timeout=20)
            sent = "Sent to Discord." if r.status_code < 300 else f"Discord said HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            sent = f"Couldn't reach Discord: {e}"
    try:
        print(msg)
    except Exception:
        print(msg.encode("ascii", "replace").decode())
    print(sent)


if __name__ == "__main__":
    main()
