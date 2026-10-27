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
                    out.append(dict(dong=g("umdNm"), apt=norm(g("aptNm")), area=float(g("excluUseAr")),
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

def fill_prev(items, key):
    by = {}
    for r in items: by.setdefault(r["_lawd"], None)
    for lawd in by:
        by[lawd] = molit_deals(key, lawd)
        print("실거래", lawd, len(by[lawd]), "건", flush=True)
    hit = 0
    for r in items:
        a = norm(r["name"]); cand = []
        for d in by[r["_lawd"]]:
            if d["dong"] != r["dong"] or abs(d["area"] - r["_excl"]) > 1.0: continue
            if d["apt"] == a or (len(a) >= 3 and len(d["apt"]) >= 3 and (a in d["apt"] or d["apt"] in a)):
                cand.append(d)
        if cand:
            last = max(cand, key=lambda d: d["ymd"])
            r["prev"], r["prev_date"] = round(last["amt"], 2), last["ymd"]; hit += 1
    print(f"직전 매매가 매칭 {hit}/{len(items)}", flush=True)

def label(sido, gu, cname):
    if "위례" in cname: return "위례"
    if gu.startswith("성남시"): return "성남 " + gu.replace("성남시", "").strip().rstrip("구")
    if gu.startswith("하남시"): return "하남"
    return gu.rstrip("구")

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

    best, seen = {}, set()
    year = dt.datetime.now(KST).year
    t0 = time.time(); stop = False
    for gname, gno in guList:
        if stop: break
        for dong in regions(s, gno):
            if time.time() - t0 > TIME_LIMIT_MIN * 60:
                print("시간 제한 도달: 지금까지 결과를 저장합니다", flush=True); stop = True; break
            d = get(s, "/api/regions/complexes", cortarNo=dong["cortarNo"], realEstateType="APT", order="")
            for c in (d or {}).get("complexList", []):
                no = c["complexNo"]
                if no in seen or c.get("totalHouseholdCount", 0) < MIN_HOUSEHOLDS: continue
                seen.add(no)
                page = 1
                while True:
                    a = get(s, f"/api/articles/complex/{no}", realEstateType="APT", tradeType="A1",
                            order="prc", page=page, complexNo=no)
                    if not a: break
                    for x in a.get("articleList", []):
                        tags = " ".join(x.get("tagList", [])) + " " + x.get("articleFeatureDesc", "")
                        if "급매" not in tags: continue
                        py = round(float(x["area1"]) / 3.3058)
                        price = parse_price(x["dealOrWarrantPrc"])
                        key = (no, py)
                        if key in best and best[key]["price"] <= price: continue
                        st, dist = nearest(sts, float(c["latitude"]), float(c["longitude"]))
                        ymd = str(c.get("useApproveYmd") or "")
                        best[key] = dict(region=label("", gname, c["complexName"]),
                            city=gname[:2] if gname.startswith(("성남","하남")) else "서울",
                            gu=gname.replace("성남시 ","").strip(),
                            age=(year - int(ymd[:4])) if ymd[:4].isdigit() else None,
                            name=c["complexName"], py=py, price=price, prev=None, prev_date=None, dong=dong["cortarName"],
                            _excl=float(x.get("area2") or 0), _lawd=gno[:5],
                            units=c.get("totalHouseholdCount"), subway=st, dist_m=dist)
                    if not a.get("isMoreData"): break
                    page += 1
            print(gname, dong["cortarName"], "누적 급매", len(best), flush=True)
    items = sorted(best.values(), key=lambda r: r["price"])
    key = os.environ.get("MOLIT_KEY", "").strip()
    if key and items: fill_prev(items, key)
    else: print("MOLIT_KEY 없음: 직전 매매가는 비워 둡니다", flush=True)
    for r in items:
        r.pop("_excl", None); r.pop("_lawd", None)
    out = {"updated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), "items": items}
    if not items: sys.exit("급매 0건: 수집 실패로 보고 data.json을 덮어쓰지 않음")
    json.dump(out, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장", len(items))

if __name__ == "__main__":
    main()
