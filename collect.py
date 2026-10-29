"""네이버부동산 급매 호가 수집 -> data.json (서울/성남/하남/위례).
개인용·저속 수집 전제. 네이버 내부 API는 예고 없이 바뀔 수 있음(첫 실행 로그로 확인)."""
import re, json, math, time, random, sys, os, datetime as dt
import requests
import xml.etree.ElementTree as ET
from curl_cffi import requests as cr

MIN_HOUSEHOLDS = 1000    # 이 세대수 미만 단지는 건너뜀(요청 수 절감)
TARGET_GU = None         # 예: ["송파구","성남시 수정구","하남시"]  None이면 전체
TIME_LIMIT_MIN = 150     # 이 시간이 지나면 거기까지 모은 결과를 저장하고 종료
SLEEP = (0.7, 1.4)       # 요청 간 대기(초)
BASE = "https://new.land.naver.com"
KST = dt.timezone(dt.timedelta(hours=9))

def nap(): time.sleep(random.uniform(*SLEEP))

def session():
    s = cr.Session(impersonate="chrome")
    s.headers.update({"Referer": BASE + "/", "Accept-Language": "ko-KR,ko;q=0.9"})
    html = s.get(BASE + "/").text
    m = re.search(r'"token"\s*:\s*"(eyJ[^"]+)"', html)
    if not m:
        sys.exit("토큰을 찾지 못함: 네이버가 접속을 막았거나 페이지 구조가 바뀜")
    s.headers["Authorization"] = "Bearer " + m.group(1)
    return s

def get(s, path, **params):
    nap()
    r = s.get(BASE + path, params=params)
    if r.status_code != 200:
        print("HTTP", r.status_code, path, params); return None
    return r.json()

def regions(s, cortar):
    d = get(s, "/api/regions/list", cortarNo=cortar)
    return (d or {}).get("regionList", [])

def parse_price(txt):  # "12억 5,000" -> 12.5
    eok, man = 0, 0
    m = re.search(r"(\d+)억", txt)
    if m: eok = int(m.group(1))
    rest = re.sub(r".*억", "", txt).replace(",", "").strip()
    if rest.isdigit(): man = int(rest)
    return round(eok + man / 10000, 2)

def haversine(la1, lo1, la2, lo2):
    p = math.pi / 180
    a = (math.sin((la2-la1)*p/2)**2 + math.cos(la1*p)*math.cos(la2*p)*math.sin((lo2-lo1)*p/2)**2)
    return 2 * 6371000 * math.asin(math.sqrt(a))

def stations():
    """역 좌표는 stations.json에 캐시(30일). 못 받아도 거리 없이 계속 진행."""
    try:
        if time.time() - os.path.getmtime("stations.json") < 30 * 86400:
            return [tuple(x) for x in json.load(open("stations.json", encoding="utf-8"))]
    except OSError:
        pass
    q = """[out:json][timeout:60];(
      node["railway"="station"]["station"="subway"](37.35,126.75,37.75,127.30);
      node["railway"="station"]["subway"="yes"](37.35,126.75,37.75,127.30);
    );out;"""
    hdr = {"User-Agent": "gupmae-personal/1.0 (personal use)", "Accept": "application/json"}
    for rnd in range(3):
        for url in ("https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter"):
            try:
                r = requests.post(url, data={"data": q}, headers=hdr, timeout=60)
                r.raise_for_status()
                st = [(e["tags"].get("name", "?"), e["lat"], e["lon"]) for e in r.json()["elements"]]
                json.dump(st, open("stations.json", "w", encoding="utf-8"), ensure_ascii=False)
                return st
            except Exception as e:
                print("Overpass 실패", url, e, flush=True)
        time.sleep(30 * (rnd + 1))
    print("역 좌표를 못 받아 지하철 거리 없이 진행합니다", flush=True)
    return []

def nearest(sts, la, lo):
    if not sts: return None, None
    n, d = min(((n, haversine(la, lo, a, b)) for n, a, b in sts), key=lambda x: x[1])
    return n, int(d)


MOLIT_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
PREV_MONTHS = 12   # 직전 매매가를 찾아볼 기간(개월)

def norm(n):
    return re.sub(r"[\s()\[\]·\-]|아파트|단지", "", n or "")

def molit_deals(key, lawd):
    """시군구(lawd) 최근 PREV_MONTHS개월 매매 신고분(해제 제외)."""
    out = []
    today = dt.datetime.now(KST)
    y, m = today.year, today.month
    for _ in range(PREV_MONTHS):
        ym = f"{y}{m:02d}"; page = 1
        while True:
            nap()
            try:
                r = requests.get(MOLIT_URL, params=dict(serviceKey=key, LAWD_CD=lawd, DEAL_YMD=ym,
                                 numOfRows=1000, pageNo=page), timeout=30)
                root = ET.fromstring(r.content)
            except Exception as e:
                print("국토부 API 오류", lawd, ym, e, flush=True); break
            code = (root.findtext(".//resultCode") or "").strip()
            if code not in ("000", "00"):
                print("국토부 API 응답", lawd, ym, code, root.findtext(".//resultMsg"), flush=True); break
            for it in root.iter("item"):
                g = lambda k: (it.findtext(k) or "").strip()
                if g("cdealType") == "O": continue   # 해제(취소) 건 제외
                try:
                    fl = re.sub(r"\D", "", g("floor"))
                    out.append(dict(dong=g("umdNm"), apt=norm(g("aptNm")), area=float(g("excluUseAr")),
                        floor=int(fl) if fl else None,
                        amt=int(g("dealAmount").replace(",", "")) / 10000,
                        ymd=f"{int(g('dealYear')):04d}-{int(g('dealMonth')):02d}-{int(g('dealDay')):02d}"))
                except ValueError:
                    pass
            total = int(root.findtext(".//totalCount") or 0)
            if page * 1000 >= total: break
            page += 1
        m -= 1
        if m == 0: y, m = y - 1, 12
    return out

def name_match(a, b):
    return a == b or (len(a) >= 3 and len(b) >= 3 and (a in b or b in a))

def fill_prev(items, deals):
    hit = 0
    for r in items:
        a = norm(r["name"])
        cand = [d for d in deals.get(r["_lawd"], []) if d["dong"] == r["dong"]
                and abs(d["area"] - r["_excl"]) <= 1.0 and name_match(a, d["apt"])]
        if cand:
            last = max(cand, key=lambda d: d["ymd"])
            r["prev"], r["prev_date"] = round(last["amt"], 2), last["ymd"]; hit += 1
    print(f"직전 매매가 매칭 {hit}/{len(items)}", flush=True)

def floor_no(f):
    m = re.match(r"\s*(\d+)\s*/", f or "")
    return int(m.group(1)) if m else None

SOLD_WINDOW = 90   # 소멸 후 이 기간(일) 동안 실거래 신고가 올라오는지 계속 대조

def match_sold(units, deals, today):
    """소멸한 호실을 실거래 신고(단지·동·전용면적±1㎡·층·계약일>=최초 포착일)와 대조. 층이 불명확하면 대조하지 않음."""
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=SOLD_WINDOW)).isoformat()
    n = 0
    for u in units.values():
        if u["active"] or u.get("sold") or (u["gone_date"] or "") < cutoff: continue
        fl = floor_no(u.get("floor"))
        if fl is None: continue
        a = norm(u["name"])
        cand = [d for d in deals.get(u["lawd"], []) if d["dong"] == u["dong"] and d["floor"] == fl
                and abs(d["area"] - u["excl"]) <= 1.0 and d["ymd"] >= u["first_seen"] and name_match(a, d["apt"])]
        if cand:
            d = min(cand, key=lambda d: d["ymd"]); u["sold"] = dict(ymd=d["ymd"], amt=round(d["amt"], 2)); n += 1
    print("거래 추정 신규 매칭", n, flush=True)

def label(sido, gu, cname):
    if "위례" in cname: return "위례"
    if gu.startswith("성남시"): return "성남 " + gu.replace("성남시", "").strip().rstrip("구")
    if gu.startswith("하남시"): return "하남"
    return gu.rstrip("구")

HIST = "history"
KEEP_DAYS = 400

def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return default

def save_json(p, obj, **kw):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"), **kw)

def update_history(units, daily, seen, scanned, stat, today):
    """매물(호실 단위)의 등록일/가격변동/소멸을 누적하고, 구별 일일 통계를 만든다.
    같은 날 여러 번 돌려도 결과가 같도록(멱등) 오늘 통계는 units에서 다시 계산한다."""
    for k, r in seen.items():
        u = units.get(k)
        if u is None:
            units[k] = dict(r, first_seen=today, last_seen=today, first_price=r["price"],
                            ph=[[today, r["price"]]], active=True, gone_date=None, relists=0)
            continue
        if not u["active"]:
            u["relists"] = u.get("relists", 0) + 1; u["active"] = True; u["gone_date"] = None
        for f in ("name", "region", "city", "gu", "dong", "py", "age", "households", "subway", "dist_m", "excl", "lawd", "cno"):
            u[f] = r[f]
        u["last_seen"] = today
        if u["ph"][-1][1] != r["price"]:
            if u["ph"][-1][0] == today: u["ph"][-1][1] = r["price"]
            else: u["ph"].append([today, r["price"]])
    for k, u in units.items():                       # 오늘 스캔한 단지에서 안 보이면 소멸 처리
        if u["active"] and k not in seen and str(u["cno"]) in scanned:
            u["active"] = False; u["gone_date"] = today
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for k in [k for k, u in units.items() if not u["active"] and (u["gone_date"] or "") < cutoff]:
        del units[k]
    by = {gk: dict(c=v["c"], s=v["s"], t=v["t"], m=0, n=0, x=0, u=0, p=0) for gk, v in stat.items()}
    pp = {}
    for u in units.values():
        e = by.get(u["city"] + " " + u["gu"])
        if e is None: continue
        if u["active"] and u["last_seen"] == today:
            e["m"] += 1; pp.setdefault(u["city"] + " " + u["gu"], []).append(u["ph"][-1][1] * 10000 / u["py"])
        if u["first_seen"] == today: e["n"] += 1
        if u["gone_date"] == today: e["x"] += 1
        if len(u["ph"]) > 1 and u["ph"][-1][0] == today and u["ph"][-1][1] < u["ph"][-2][1]: e["u"] += 1
    for gk, v in pp.items(): by[gk]["p"] = round(sum(v) / len(v))   # 평균 평당 호가(만원)
    daily[today] = by
    for d in sorted(daily)[:-KEEP_DAYS]: del daily[d]

def main():
    s = session()
    sts = stations(); print("지하철역", len(sts))
    top = {r["cortarName"]: r["cortarNo"] for r in regions(s, "0000000000")}
    guList = []
    for sido in ("서울시", "경기도"):
        if sido not in top: sys.exit(f"시도 목록에 {sido} 없음: {list(top)}")
        for g in regions(s, top[sido]):
            ok = sido == "서울시" or g["cortarName"].startswith(("성남시", "하남시"))
            if ok and (TARGET_GU is None or g["cortarName"] in TARGET_GU):
                guList.append((g["cortarName"], g["cortarNo"]))
    print("구/시", len(guList))

    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    units = load_json(f"{HIST}/units.json", {})
    daily = load_json(f"{HIST}/daily.json", {})
    seen, scanned, stat, seen_c = {}, set(), {}, set()
    year = dt.datetime.now(KST).year
    t0 = time.time(); stop = False
    for gname, gno in guList:
        if stop: break
        city = gname[:2] if gname.startswith(("성남", "하남")) else "서울"
        gu = gname.replace("성남시 ", "").strip()
        st_ = stat.setdefault(f"{city} {gu}", dict(c=city, s=0, t=0))
        for dong in regions(s, gno):
            if time.time() - t0 > TIME_LIMIT_MIN * 60:
                print("시간 제한 도달: 지금까지 결과를 저장합니다", flush=True); stop = True; break
            d = get(s, "/api/regions/complexes", cortarNo=dong["cortarNo"], realEstateType="APT", order="")
            for c in (d or {}).get("complexList", []):
                no = c["complexNo"]
                if no in seen_c or c.get("totalHouseholdCount", 0) < MIN_HOUSEHOLDS: continue
                seen_c.add(no)
                page, ok, total, found = 1, True, 0, []
                while True:
                    a = get(s, f"/api/articles/complex/{no}", realEstateType="APT", tradeType="A1",
                            order="prc", page=page, complexNo=no)
                    if not a: ok = False; break
                    lst = a.get("articleList", [])
                    total += len(lst)
                    for x in lst:
                        tags = " ".join(x.get("tagList", [])) + " " + x.get("articleFeatureDesc", "")
                        if "급매" in tags: found.append(x)
                    if not a.get("isMoreData"): break
                    page += 1
                if not ok: continue            # 끝까지 못 읽은 단지는 '소멸' 판정에서 제외
                scanned.add(str(no)); st_["s"] += 1; st_["t"] += total
                ymd = str(c.get("useApproveYmd") or "")
                for x in found:
                    py = round(float(x["area1"]) / 3.3058)
                    price = parse_price(x["dealOrWarrantPrc"])
                    k = f'{no}|{x.get("buildingName","")}|{x.get("floorInfo","")}|{x.get("area2","")}|{x.get("direction","")}'
                    if k in seen and seen[k]["price"] <= price: continue   # 같은 호실을 여러 중개사가 올린 경우 최저가
                    sub, dist = nearest(sts, float(c["latitude"]), float(c["longitude"]))
                    seen[k] = dict(cno=no, region=label("", gname, c["complexName"]), city=city, gu=gu, dong=dong["cortarName"],
                        name=c["complexName"], py=py, price=price, excl=float(x.get("area2") or 0), lawd=gno[:5],
                        age=(year - int(ymd[:4])) if ymd[:4].isdigit() else None,
                        households=c.get("totalHouseholdCount"), subway=sub, dist_m=dist,
                        bld=x.get("buildingName", ""), floor=x.get("floorInfo", ""))
            print(gname, dong["cortarName"], "오늘 급매", len(seen), flush=True)
    if not seen: sys.exit("급매 0건: 수집 실패로 보고 저장하지 않음")

    update_history(units, daily, seen, scanned, stat, today)
    save_json(f"{HIST}/units.json", units)
    save_json(f"{HIST}/daily.json", daily)

    # 화면용: 단지+평형별로 현재 최저가 매물 1건만 대표로 (n = 그 단지·평형의 급매 호실 수)
    grp = {}
    for u in units.values():
        if not u["active"]: continue
        g = grp.setdefault((u["cno"], u["py"]), [])
        g.append(u)
    items = []
    for lst in grp.values():
        u = min(lst, key=lambda u: u["ph"][-1][1])
        price = u["ph"][-1][1]
        items.append(dict(cno=u["cno"], region=u["region"], city=u["city"], gu=u["gu"], dong=u["dong"], age=u["age"], name=u["name"],
            py=u["py"], price=price, prev=None, prev_date=None, units=u["households"], subway=u["subway"],
            dist_m=u["dist_m"], n=len(lst), fv=int(any(floor_no(x.get("floor")) is None for x in lst)),
            floor=u.get("floor", ""), first_seen=u["first_seen"], price0=u["first_price"],
            ph=u["ph"], relists=u.get("relists", 0), _excl=u["excl"], _lawd=u["lawd"]))
    items.sort(key=lambda r: r["price"])
    key = os.environ.get("MOLIT_KEY", "").strip()
    if key:
        gcut = (dt.date.fromisoformat(today) - dt.timedelta(days=SOLD_WINDOW)).isoformat()
        need = {u["lawd"] for u in units.values() if u["active"] or (u["gone_date"] or "") >= gcut}
        deals = {l: molit_deals(key, l) for l in sorted(need)}
        for l, v in deals.items(): print("실거래", l, len(v), "건", flush=True)
        match_sold(units, deals, today)
        fill_prev(items, deals)
        save_json(f"{HIST}/units.json", units)       # sold 결과 반영
    else: print("MOLIT_KEY 없음: 직전 매매가·거래 추정은 비워 둡니다", flush=True)
    for r in items:
        r.pop("_excl", None); r.pop("_lawd", None)
    gcut = (dt.date.fromisoformat(today) - dt.timedelta(days=SOLD_WINDOW)).isoformat()
    gone = [dict(cno=u["cno"], region=u["region"], city=u["city"], gu=u["gu"], name=u["name"], py=u["py"], floor=u.get("floor", ""),
                 fv=0 if floor_no(u.get("floor")) is not None else 1, first_seen=u["first_seen"], gone_date=u["gone_date"],
                 price0=u["first_price"], price=u["ph"][-1][1], sold=u.get("sold"))
            for u in units.values() if not u["active"] and (u["gone_date"] or "") >= gcut]
    gone.sort(key=lambda r: r["gone_date"], reverse=True)
    out = {"updated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), "items": items, "gone": gone[:600]}
    json.dump(out, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장", len(items), "건 / 누적 매물", len(units))

if __name__ == "__main__":
    main()
