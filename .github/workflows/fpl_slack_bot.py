#!/usr/bin/env python3
import os
import sys
import time
import json
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any

import requests

# ------------ Config via env vars ------------
LEAGUE_ID = os.getenv("FPL_LEAGUE_ID")  # e.g. "123456"
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")  # Slack Incoming Webhook URL
POST_CHANNEL = os.getenv("SLACK_CHANNEL", "")  # optional, if your webhook permits overriding
TEAM_LIMIT = int(os.getenv("TEAM_LIMIT", "50"))  # how many teams to scan in the league
TIMEZONE = os.getenv("LOCAL_TZ", "Europe/London")

if not LEAGUE_ID or not SLACK_WEBHOOK_URL:
    print("Missing FPL_LEAGUE_ID or SLACK_WEBHOOK_URL in environment.", file=sys.stderr)
    sys.exit(2)

BASE = "https://fantasy.premierleague.com/api"

session = requests.Session()
session.headers.update({
    "User-Agent": "FPL Slack Bot (github.com/the-inkey-list)"
})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def get_json(url: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_bootstrap() -> Dict[str, Any]:
    return get_json(f"{BASE}/bootstrap-static/")

def get_classic_standings(league_id: str, page: int = 1, phase: int = None) -> Dict[str, Any]:
    # phase filters by month-like groupings in FPL
    params = {"page_standings": page, "page_new_entries": 1}
    if phase is not None:
        params["phase"] = phase
    return get_json(f"{BASE}/leagues-classic/{league_id}/standings/", params=params)

def get_entry_history(entry_id: int) -> Dict[str, Any]:
    return get_json(f"{BASE}/entry/{entry_id}/history/")

def last_finished_gw(events: List[Dict[str, Any]]) -> int:
    finished = [e for e in events if e.get("finished")]
    if not finished:
        raise RuntimeError("No finished gameweeks yet")
    return max(e["id"] for e in finished)

def current_phase_from_date(events: List[Dict[str, Any]], now_utc: datetime) -> int:
    # FPL maps phases to blocks of GWs that roughly track calendar months.
    # We infer the phase by choosing the phase of the latest finished GW.
    gw = last_finished_gw(events)
    # Find event with id == gw
    event = next(e for e in events if e["id"] == gw)
    return int(event.get("phase", 1))

def collect_all_teams(league_id: str, phase: int = None, limit: int = 200) -> List[Dict[str, Any]]:
    teams = []
    page = 1
    while len(teams) < limit:
        data = get_classic_standings(league_id, page=page, phase=phase)
        standings = data.get("standings", {}).get("results", [])
        if not standings:
            break
        teams.extend(standings)
        if not data.get("standings", {}).get("has_next"):
            break
        page += 1
    return teams[:limit]

def build_overall_table(league_id: str, limit: int) -> List[Tuple[str, int]]:
    teams = collect_all_teams(league_id, phase=None, limit=limit)
    # Each item has 'entry' (team id), 'entry_name', 'player_name', 'total'
    ranked = sorted([(f"{t['entry_name']} ({t['player_name']})", int(t["total"])) for t in teams],
                    key=lambda x: x[1], reverse=True)
    return ranked

def build_month_table(league_id: str, phase: int, limit: int) -> List[Tuple[str, int]]:
    teams = collect_all_teams(league_id, phase=phase, limit=limit)
    # 'event_total' is cumulative points within the phase
    ranked = sorted([(f"{t['entry_name']} ({t['player_name']})", int(t["event_total"])) for t in teams],
                    key=lambda x: x[1], reverse=True)
    return ranked

def compute_bottom_of_week(league_id: str, gw: int, limit: int) -> Tuple[str, int]:
    teams = collect_all_teams(league_id, phase=None, limit=limit)
    worst_name = None
    worst_points = 10**9
    for t in teams:
        entry_id = t["entry"]
        name = f"{t['entry_name']} ({t['player_name']})"
        hist = get_entry_history(entry_id)
        this_gw = next((g for g in hist.get("current", []) if int(g["event"]) == gw), None)
        if not this_gw:
            continue
        pts = int(this_gw.get("points", 0))
        if pts < worst_points:
            worst_points = pts
            worst_name = name
    if worst_name is None:
        raise RuntimeError("Could not compute bottom of the week")
    return worst_name, worst_points

def fmt_table(rows: List[Tuple[str, int]], top_n: int = 10) -> str:
    lines = []
    width_rank = len(str(top_n))
    for i, (name, pts) in enumerate(rows[:top_n], start=1):
        lines.append(f"{i:>{width_rank}}. {name} - {pts}")
    return "\n".join(lines)

def post_to_slack(text: str, blocks: List[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"text": text}
    if POST_CHANNEL:
        payload["channel"] = POST_CHANNEL
    if blocks:
        payload["blocks"] = blocks
    r = session.post(SLACK_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code >= 300:
        logging.error("Slack webhook error %s: %s", r.status_code, r.text)
        r.raise_for_status()

def main() -> None:
    now = datetime.now(timezone.utc)
    boot = get_bootstrap()
    events = boot.get("events", [])
    if not events:
        raise RuntimeError("No events data")

    gw = last_finished_gw(events)
    phase = current_phase_from_date(events, now)

    overall = build_overall_table(LEAGUE_ID, TEAM_LIMIT)
    monthly = build_month_table(LEAGUE_ID, phase, TEAM_LIMIT)
    worst_name, worst_points = compute_bottom_of_week(LEAGUE_ID, gw, TEAM_LIMIT)

    gw_name = next(e["name"] for e in events if e["id"] == gw)
    phase_name = next((p.get("name") for p in boot.get("phases", []) if int(p.get("id")) == phase), f"Phase {phase}")

    header = f"FPL update - {gw_name} complete"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Monthly table* - {phase_name}\n```{fmt_table(monthly, top_n=10)}```"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Overall table*\n```{fmt_table(overall, top_n=10)}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Bottom of the week*: {worst_name} with {worst_points} pts"}]},
    ]

    post_to_slack(text=header, blocks=blocks)
    logging.info("Posted Slack update for %s", header)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("Run failed: %s", e)
        # Send failure to Slack so someone sees it
        try:
            post_to_slack(text=f"FPL bot failed: {e}")
        except Exception:
            pass
        sys.exit(1)
