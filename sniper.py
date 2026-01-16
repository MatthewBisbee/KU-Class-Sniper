import re
import gzip
import time
import json
import urllib.parse
import urllib.request
import urllib.error
import http.client
from datetime import datetime

#This version was made around the last specific section I needed
#To snipe your course seat, change values accordingly and use your own Pushover (iOS app) keys
#Script development assisted by OpenAI's ChatGPT 5.1

# CONFIG
SEARCH_URL = "https://classes.ku.edu/Classes/CourseSearch.action"
CLASS_NUMBER = "40525"
COURSE_LABEL = "EECS 212 Monday lab"

# Polling timing
STARTUP_DELAY_SECONDS = 3
POLL_INTERVAL_SECONDS = 242

# Logging / state
LOG_PATH = "seatwatch.log"
STATE_PATH = "seatwatch_state.json"

# Pushover (My keys are redacted, insert your own from Pushover app if adapting for yourself)
PUSHOVER_USER_KEY = "..."
PUSHOVER_APP_TOKEN = "..."
ENABLE_PUSHOVER = True

# This POST body is from your captured XHR
FORM = {
    "classesSearchText": "eecs 212",
    "searchCareer": "Undergraduate",
    "searchTerm": "4262",
    "searchSchool": "",
    "searchDept": "",
    "searchSubject": "",
    "searchCode": "",
    "textbookOptions": "",
    "searchCampus": "",
    "searchBuilding": "",
    "searchCourseNumberMin": "001",
    "searchCourseNumberMax": "999",
    "searchCreditHours": "",
    "searchInstructor": "",
    "searchStartTime": "",
    "searchEndTime": "",
    "searchClosed": "false",
    "searchHonorsClasses": "false",
    "searchShortClasses": "false",
    "searchOnlineClasses": "",
    "searchIncludeExcludeDays": "include",
    "searchDays": "",
}

# Optional cookies: leave blank unless you start getting empty/blocked responses.
COOKIE = ""

# Backoff for errors / throttling
BACKOFF_INITIAL = 30
BACKOFF_MAX = 1800


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def log_line(s):
    line = now_iso() + " | " + s
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
            # If coming from the two-class version, prefer the primary field.
            if isinstance(s, dict):
                if "last_seats" in s:
                    last_seats = s.get("last_seats", None)
                elif "last_212" in s:
                    last_seats = s.get("last_212", None)
                else:
                    last_seats = None
                return {"last_seats": last_seats, "last_seen": s.get("last_seen", None)}
    except Exception:
        pass
    return {"last_seats": None, "last_seen": None}


def save_state(seats):
    state = {"last_seats": seats, "last_seen": now_iso()}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def pushover_send(title, message):
    if not ENABLE_PUSHOVER:
        return None, None

    payload = urllib.parse.urlencode({
        "token": PUSHOVER_APP_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": title,
        "message": message,
    })

    headers = {
        "Content-type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(payload)),
    }

    conn = http.client.HTTPSConnection("api.pushover.net", 443, timeout=10)
    conn.request("POST", "/1/messages.json", payload, headers)
    resp = conn.getresponse()
    body = resp.read().decode(errors="replace")
    conn.close()
    return resp.status, body


def read_response(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding", "") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def fetch_search_response():
    data = urllib.parse.urlencode(FORM).encode("utf-8")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KU-Seatwatch/1.0",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://classes.ku.edu",
        "Referer": "https://classes.ku.edu/",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }
    if COOKIE.strip():
        headers["Cookie"] = COOKIE.strip()

    req = urllib.request.Request(SEARCH_URL, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = getattr(resp, "status", 200)
            text = read_response(resp)
            return status, text, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body, "HTTPError"
    except Exception as e:
        return None, "", str(e)


def extract_seats(text, class_number):
    idx = text.find(class_number)
    if idx == -1:
        return None, "Class number not found in response."

    window = text[idx:idx + 12000]

    m = re.search(r">\s*(Full|[0-9]{1,3})\s*<", window, flags=re.IGNORECASE)
    if not m:
        return None, "No seat-like cell token found near class number."

    token = m.group(1).strip()
    if token.lower() == "full":
        return 0, None

    try:
        return int(token), None
    except Exception:
        return None, "Seat token was not parseable."


def seat_message(seats):
    return str(seats) + " seat(s) open in " + COURSE_LABEL


def main():
    state = load_state()
    last_seats = state.get("last_seats", None)

    log_line("Starting. Target class=" + CLASS_NUMBER + " (" + COURSE_LABEL + ")")
    log_line("Startup delay=" + str(STARTUP_DELAY_SECONDS) + "s, interval=" + str(POLL_INTERVAL_SECONDS) + "s")
    log_line("Last known seats=" + str(last_seats))

    time.sleep(STARTUP_DELAY_SECONDS)

    backoff = BACKOFF_INITIAL
    sent_startup = False

    while True:
        status, text, err = fetch_search_response()

        if status in [403, 429]:
            log_line("ERROR: HTTP " + str(status) + " (possible block/rate limit). Backing off " + str(backoff) + "s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if status is None:
            log_line("ERROR: request failed (" + str(err) + "). Backing off " + str(backoff) + "s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if status >= 500:
            log_line("ERROR: HTTP " + str(status) + " (server error). Backing off " + str(backoff) + "s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        backoff = BACKOFF_INITIAL

        seats, parse_err = extract_seats(text, CLASS_NUMBER)
        if seats is None:
            log_line("ERROR: parse failed (" + str(parse_err) + "). HTTP " + str(status) +
                     " chars=" + str(len(text)) + ". Backing off " + str(backoff) + "s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if last_seats is None:
            log_line("Seats=" + str(seats) + " (initial). HTTP " + str(status) + " chars=" + str(len(text)))
        elif seats != last_seats:
            log_line("Seats=" + str(seats) + " (changed from " + str(last_seats) + "). HTTP " + str(status) +
                     " chars=" + str(len(text)))
        else:
            log_line("Seats=" + str(seats) + ". HTTP " + str(status) + " chars=" + str(len(text)))

        if not sent_startup:
            ps, body = pushover_send("Now polling for open seat", seat_message(seats))
            log_line("Startup notify: Pushover HTTP " + str(ps) + " " + str(body))
            sent_startup = True

        if last_seats is not None and last_seats == 0 and seats > 0:
            ps, body = pushover_send("Seat open!", seat_message(seats))
            log_line("Open-seat notify: Pushover HTTP " + str(ps) + " " + str(body))

        last_seats = seats
        save_state(seats)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
