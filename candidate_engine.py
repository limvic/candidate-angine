"""
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║   CANDIDATE ENGINE  v1.1                                              ║
║   파일명: candidate_engine.py                                         ║
║                                                                       ║
║   👁  역할: 눈 — 시장 전체 스캔 → 후보 추출                           ║
║   🧠  트리니티(건눌재/안눌상/Entry)와 완전 분리                       ║
║                                                                       ║
║   채피(GPT) 설계  ×  써니(Claude) 구현  ×  림빅 최종결정             ║
║                                                                       ║
║   ─────────────────────────────────────────────────────────────────  ║
║   [아키텍처]                                                          ║
║                                                                       ║
║    ┌────────────────────┐       수동 전달        ┌─────────────────┐  ║
║    │  Candidate Engine  │  ──────────────────▶  │    Trinity      │  ║
║    │  (이 파일, 눈 👁)  │     후보 리스트 복사    │  (건눌재/안눌상  │  ║
║    │  /candidates       │                        │   Entry, 뇌 🧠) │  ║
║    │  /candidates/log   │                        │  /kunnuljae     │  ║
║    │  /health           │                        │  /annulsang     │  ║
║    └────────────────────┘                        └─────────────────┘  ║
║                                                                       ║
║    현재: 연결 없음. 수동 운용.                                         ║
║    v1.2 예정: Candidate → 건눌재 자동 전달 파이프라인                  ║
║                                                                       ║
║   ─────────────────────────────────────────────────────────────────  ║
║   [배포]                                                              ║
║    Railway 새 프로젝트 or 별도 서비스로 배포                           ║
║    환경변수: KIS_APP_KEY / KIS_APP_SECRET / KIS_MODE                  ║
║                                                                       ║
║   [엔드포인트]                                                        ║
║    GET /              → 서비스 정보 + 연동 가이드                     ║
║    GET /health        → 헬스체크                                      ║
║    GET /candidates    → 후보 스캔 (limit=10 or 20)                    ║
║    GET /candidates/log → 복기 로그 (last=N)                           ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════════
# IMPORTS
# ═══════════════════════════════════════════════════════════════════════

import asyncio
import datetime
import os
import time
from collections import deque
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ═══════════════════════════════════════════════════════════════════════
# APP 초기화
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Candidate Engine",
    description="Trinity 1차 후보 스캐너 — 눈(👁). 트리니티 뇌(🧠)와 분리된 독립 서비스.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════
# 환경변수 — Railway 환경변수에서 읽기
# ═══════════════════════════════════════════════════════════════════════

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY",    "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_MODE       = os.environ.get("KIS_MODE",       "mock")   # "mock" | "real"

# 토큰 캐시 (만료 전까지 재사용)
_token_cache: dict = {"token": "", "expires_at": 0.0}


def get_access_token() -> str:
    """KIS OAuth 토큰 발급 (캐시 포함, 만료 10분 전 재발급)."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 600:
        return _token_cache["token"]

    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey":     KIS_APP_KEY,
        "appsecret":  KIS_APP_SECRET,
    }
    import requests
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 86400))
    return _token_cache["token"]


# ═══════════════════════════════════════════════════════════════════════
# 상수
# ═══════════════════════════════════════════════════════════════════════

SCANNER_TYPE = "1차 후보 스캐너"
SCANNER_NOTE = (
    "MA5/MA20/20일고가는 proxy값(v1). "
    "정밀 진입 판정은 v2(일봉 API 연동) 이후에만 신뢰 가능. "
    "이 결과를 트리니티(건눌재/안눌상)에 수동으로 넣어 판단하세요."
)

# 섹터 선도주 — 촉매 점수 +10
SECTOR_LEADERS = {
    "267250",   # HD현대일렉트릭
    "042700",   # 한미반도체
    "000660",   # SK하이닉스
    "336260",   # 이수페타시스
    "114120",   # 주성엔지니어링
    "058470",   # 리노공업
    "039030",   # 이오테크닉스
    "095340",   # ISC
}

# 스캔 복기 로그 — 최근 20회치 메모리 보관
_scan_log_store: deque = deque(maxlen=20)

# ═══════════════════════════════════════════════════════════════════════
# 스캔 유니버스 (v1: 30종목 고정)
# v2: KIS 거래량 상위 API로 동적 확장 예정
# ═══════════════════════════════════════════════════════════════════════

CANDIDATE_UNIVERSE = [
    # 대형주
    {"code": "005930", "name": "삼성전자"},
    {"code": "000660", "name": "SK하이닉스"},
    {"code": "035420", "name": "NAVER"},
    {"code": "051910", "name": "LG화학"},
    {"code": "006400", "name": "삼성SDI"},
    {"code": "035720", "name": "카카오"},
    {"code": "207940", "name": "삼성바이오로직스"},
    {"code": "005380", "name": "현대차"},
    {"code": "000270", "name": "기아"},
    {"code": "105560", "name": "KB금융"},
    {"code": "055550", "name": "신한지주"},
    {"code": "003550", "name": "LG"},
    {"code": "096770", "name": "SK이노베이션"},
    {"code": "066570", "name": "LG전자"},
    {"code": "017670", "name": "SK텔레콤"},
    {"code": "032830", "name": "삼성생명"},
    # 림빅 쿼드코어 + 위성
    {"code": "267250", "name": "HD현대일렉트릭"},
    {"code": "042700", "name": "한미반도체"},
    {"code": "336260", "name": "이수페타시스"},
    {"code": "114120", "name": "주성엔지니어링"},
    # 반도체 장비/소재
    {"code": "140860", "name": "파크시스템스"},
    {"code": "357780", "name": "솔브레인"},
    {"code": "095340", "name": "ISC"},
    {"code": "058470", "name": "리노공업"},
    {"code": "039030", "name": "이오테크닉스"},
    # 기타 관심군
    {"code": "012330", "name": "현대모비스"},
    {"code": "009830", "name": "한화솔루션"},
    {"code": "010140", "name": "삼성중공업"},
    {"code": "047050", "name": "포스코인터내셔널"},
    {"code": "034020", "name": "두산에너빌리티"},
]


# ═══════════════════════════════════════════════════════════════════════
# § 1.  시세 조회 — Mock / KIS Real 자동 분기
# ═══════════════════════════════════════════════════════════════════════

def _mock_data(code: str) -> dict:
    """
    5분 단위 고정 시드 랜덤 데이터.
    KIS_MODE != 'real' 일 때 사용.
    """
    import random
    rng = random.Random(hash(code) + int(time.time() // 300))

    base     = rng.randint(10_000, 300_000)
    ma20     = int(base * rng.uniform(0.88, 1.06))
    ma5      = int(base * rng.uniform(0.97, 1.07))
    vol_prev = rng.randint(100_000, 3_000_000)

    if rng.random() < 0.30:           # 30% → 양호한 종목 시나리오
        base      = int(ma20 * rng.uniform(1.01, 1.08))
        ma5       = int(base * rng.uniform(1.0, 1.04))
        vol_today = int(vol_prev * rng.uniform(1.6, 3.5))
        change_r  = round(rng.uniform(2.0, 6.0), 2)
        vwap      = int(base * rng.uniform(0.97, 1.0))
        high_20d  = int(base * rng.uniform(1.0, 1.03))
    else:
        vol_today = int(vol_prev * rng.uniform(0.5, 1.4))
        change_r  = round(rng.uniform(-3.0, 3.0), 2)
        vwap      = int(base * rng.uniform(0.96, 1.04))
        high_20d  = int(base * rng.uniform(1.02, 1.18))

    return {
        "code": code,
        "price": base,
        "prev_close": int(base / (1 + change_r / 100)),
        "change_rate": change_r,
        "volume": vol_today, "prev_volume": vol_prev,
        "ma5": ma5, "ma20": ma20,
        "high": int(base * 1.02), "low": int(base * 0.98),
        "vwap": vwap, "high_20d": high_20d,
        "low_20d": int(base * rng.uniform(0.85, 0.99)),
        "_data_quality": "proxy",   # mock은 전부 proxy
    }


async def _fetch_one(code: str, token: str, is_mock: bool) -> Optional[dict]:
    """단일 종목 시세 조회. 실패 시 None 반환."""
    if is_mock:
        return _mock_data(code)

    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey":    KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id":     "FHKST01010100",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers=headers, params=params)
        if r.status_code != 200:
            return None
        d = r.json().get("output", {})
        if not d:
            return None

        price     = int(d.get("stck_prpr", 0) or 0)
        prev_cl   = int(d.get("stck_sdpr", 0) or 0)
        vol       = int(d.get("acml_vol",  0) or 0)
        vol_rate  = float(d.get("prdy_vrss_vol_rate", 100) or 100)
        prev_vol  = int(vol / (vol_rate / 100)) if vol_rate > 0 else vol
        high      = int(d.get("stck_hgpr", price) or price)
        low       = int(d.get("stck_lwpr", price) or price)
        vwap      = float(d.get("wghn_avrg_stck_prc", price) or price)
        change_r  = float(d.get("prdy_ctrt", 0) or 0)

        # ⚠️  현재가 API에서 MA/20일고가 직접 조회 불가
        #      v1: VWAP 기반 proxy 사용
        #      v2: FHKST03010100(일봉) 별도 조회로 exact 교체 예정
        ma20 = int(vwap * 0.99)    # proxy
        ma5  = int(vwap * 1.005)   # proxy

        return {
            "code": code, "price": price, "prev_close": prev_cl,
            "change_rate": change_r, "volume": vol, "prev_volume": prev_vol,
            "ma5": ma5, "ma20": ma20,
            "high": high, "low": low,
            "vwap": int(vwap),
            "high_20d": high,   # proxy: 당일 고가로 근사
            "low_20d": low,
            "_data_quality": "proxy",
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# § 2.  보조 판단 함수 (채피 스펙)
# ═══════════════════════════════════════════════════════════════════════

def _higher_low(d: dict) -> bool:
    """상승 저점 구조: price > ma5 > ma20"""
    return d["price"] > d["ma5"] > d["ma20"]

def _near_breakout(d: dict) -> bool:
    """현재가 >= 20일 최고가 * 0.95"""
    return d["price"] >= d["high_20d"] * 0.95

def _volume_spike(d: dict) -> bool:
    """당일 거래량 >= 전일 * 1.5"""
    return d["prev_volume"] > 0 and d["volume"] >= d["prev_volume"] * 1.5

def _not_overextended(d: dict) -> bool:
    """(현재가 - MA20) / MA20 < 10%"""
    return d["ma20"] > 0 and (d["price"] - d["ma20"]) / d["ma20"] < 0.10

def _above_vwap(d: dict) -> bool:
    """현재가 >= VWAP. VWAP 없으면 당일고가 * 0.97 대체."""
    if d.get("vwap", 0) > 0:
        return d["price"] >= d["vwap"]
    return d["high"] > 0 and d["price"] >= d["high"] * 0.97


# ═══════════════════════════════════════════════════════════════════════
# § 3.  필수 탈락 필터 — 탈락 사유 함께 반환
# ═══════════════════════════════════════════════════════════════════════

def _mandatory_filter(d: dict) -> tuple[bool, list[str]]:
    """
    채피 설계 3대 탈락 조건.
    반환: (통과, 탈락사유리스트)
    """
    reasons = []
    if d["price"] < d["ma20"]:          reasons.append("MA20 하회")
    if d["volume"] < d["prev_volume"]:  reasons.append("거래량 감소")
    if not _above_vwap(d):              reasons.append("VWAP 하회")
    return len(reasons) == 0, reasons


# ═══════════════════════════════════════════════════════════════════════
# § 4.  100점 스코어링 — score_breakdown 포함
# ═══════════════════════════════════════════════════════════════════════

def _score(d: dict) -> tuple[int, dict, list[str], list[str]]:
    """
    반환: (총점, breakdown, reason_list, risk_list)

    breakdown = {
        structure:  최대 40점  (MA20위 +15, 상승저점 +15, 돌파근접 +10)
        timing:     최대 30점  (거래량급증 +15, VWAP위 +15)
        catalyst:   최대 20점  (선도주 +10, 당일강세 +10)
        stability:  최대 10점  (이격 정상 +10)
    }
    """
    reasons, risks = [], []
    s = {"structure": 0, "timing": 0, "catalyst": 0, "stability": 0}

    # 구조 ──────────────────────────────────────────
    if d["price"] > d["ma20"]:
        s["structure"] += 15;  reasons.append("MA20 위 구조")
    if _higher_low(d):
        s["structure"] += 15;  reasons.append("상승 저점 구조")
    if _near_breakout(d):
        s["structure"] += 10;  reasons.append("돌파 근접")

    # 타이밍 ────────────────────────────────────────
    if _volume_spike(d):
        s["timing"] += 15;     reasons.append("거래량 급증")
    if _above_vwap(d):
        s["timing"] += 15;     reasons.append("VWAP 위 포지션")

    # 촉매 ──────────────────────────────────────────
    if d["code"] in SECTOR_LEADERS:
        s["catalyst"] += 10;   reasons.append("섹터 선도주")
    if d.get("change_rate", 0) >= 3.0:
        s["catalyst"] += 10;   reasons.append("당일 강세 +3%↑")

    # 안정성 ────────────────────────────────────────
    if _not_overextended(d):
        s["stability"] += 10
    else:
        risks.append("이격 주의")

    # 추가 리스크 ───────────────────────────────────
    if d["volume"] < d["prev_volume"]:   risks.append("거래량 감소")
    if d["price"] < d["ma20"] * 0.95:    risks.append("MA20 크게 하회")

    return sum(s.values()), s, reasons, risks


def _missing_hints(breakdown: dict) -> list[str]:
    """점수 미달 종목의 약점 힌트 — 복기 로그용."""
    hints = []
    if breakdown["structure"] < 30: hints.append(f"구조 점수 낮음 ({breakdown['structure']}/40)")
    if breakdown["timing"]    < 15: hints.append(f"타이밍 점수 낮음 ({breakdown['timing']}/30)")
    if breakdown["catalyst"]  == 0: hints.append("촉매 없음 (0/20)")
    return hints


# ═══════════════════════════════════════════════════════════════════════
# § 5.  상태 / 등급 판정
# ═══════════════════════════════════════════════════════════════════════

def _status(d: dict) -> str:
    p, h = d["price"], d["high_20d"]
    if p >= h:              return "돌파 중"
    if p >= h * 0.97:       return "돌파 직전"
    if p > d["ma20"] and not _volume_spike(d):
                            return "눌림 구간"
    return "상승 진행"

def _grade(score: int) -> str:
    if score >= 90: return "S"
    if score >= 80: return "A"
    if score >= 70: return "B"
    return "C"


# ═══════════════════════════════════════════════════════════════════════
# § 6.  scan_candidates() — 핵심 스캔 함수
# ═══════════════════════════════════════════════════════════════════════

async def scan_candidates(limit: int = 10) -> tuple[list[dict], dict]:
    """
    전체 유니버스 스캔 → 필터 → 점수화 → 상위 limit 반환.

    반환: (candidates, log_entry)
    """
    limit = 20 if limit >= 20 else 10

    is_mock = (KIS_MODE != "real")
    token   = ""
    if not is_mock:
        try:
            token = get_access_token()
        except Exception:
            is_mock = True   # 토큰 실패 → mock 폴백

    log_passed          : list[dict] = []
    log_rejected_filter : list[dict] = []
    log_rejected_score  : list[dict] = []
    candidates          : list[dict] = []

    for stock in CANDIDATE_UNIVERSE:
        data = await _fetch_one(stock["code"], token, is_mock)
        if data is None:
            continue
        data["name"] = stock["name"]

        if not is_mock:
            await asyncio.sleep(0.35)   # KIS rate limit 보호

        # ── 필수 탈락 필터 ──────────────────────────────────
        passed, filter_reasons = _mandatory_filter(data)
        if not passed:
            log_rejected_filter.append({
                "code": data["code"], "name": data["name"],
                "reject_reasons": filter_reasons,
            })
            continue

        # ── 점수 계산 ────────────────────────────────────────
        total, breakdown, reasons, risks = _score(data)

        if total < 70:
            log_rejected_score.append({
                "code": data["code"], "name": data["name"],
                "score": total, "breakdown": breakdown,
                "missing": _missing_hints(breakdown),
            })
            continue

        # ── 통과 ─────────────────────────────────────────────
        vol_ratio = round(data["volume"] / data["prev_volume"], 2) if data["prev_volume"] > 0 else 0

        candidates.append({
            # 기본
            "code":         data["code"],
            "name":         data["name"],
            "price":        data["price"],
            "change_rate":  data.get("change_rate", 0),
            "volume_ratio": vol_ratio,
            # 판정
            "score":        total,
            "grade":        _grade(total),
            "status":       _status(data),
            "reason":       reasons,
            "risk":         risks,
            # score_breakdown (채피 요청)
            "score_breakdown": {
                "structure":  breakdown["structure"],   # /40
                "timing":     breakdown["timing"],      # /30
                "catalyst":   breakdown["catalyst"],    # /20
                "stability":  breakdown["stability"],   # /10
            },
            # data_quality (채피 요청)
            "data_quality": {
                "overall":  data.get("_data_quality", "proxy"),
                "price":    "exact",
                "volume":   "exact",
                "vwap":     "exact" if data.get("vwap", 0) > 0 else "proxy",
                "ma5":      "proxy",    # v2에서 exact로 교체
                "ma20":     "proxy",    # v2에서 exact로 교체
                "high_20d": "proxy",    # v2에서 exact로 교체
                "note":     SCANNER_NOTE,
            },
        })

        log_passed.append({
            "code": data["code"], "name": data["name"],
            "score": total, "grade": _grade(total),
            "breakdown": breakdown,
        })

    # 점수 내림차순 → 상위 limit
    candidates.sort(key=lambda x: x["score"], reverse=True)
    result = candidates[:limit]

    # 복기 로그 저장
    log_entry = {
        "scanned_at":      datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "mode":            "real" if not is_mock else "mock",
        "limit":           limit,
        "total_scanned":   len(CANDIDATE_UNIVERSE),
        "passed_filter":   len(log_passed) + len(log_rejected_score),
        "passed_score":    len(log_passed),
        "returned":        len(result),
        "passed":          log_passed,
        "rejected_filter": log_rejected_filter,
        "rejected_score":  log_rejected_score,
    }
    _scan_log_store.append(log_entry)

    return result, log_entry


# ═══════════════════════════════════════════════════════════════════════
# § 7.  엔드포인트: GET /
#        서비스 정보 + 트리니티 연동 가이드
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return FileResponse("index.html")
    """
    Candidate Engine 서비스 정보.
    트리니티 연동 방법 가이드 포함.
    """
    return {
        "service":      "Candidate Engine",
        "version":      "1.1.0",
        "role":         "눈(👁) — 1차 후보 스캐너",
        "scanner_type": SCANNER_TYPE,
        "scanner_note": SCANNER_NOTE,
        "mode":         "real" if KIS_MODE == "real" else "mock",
        "universe_size": len(CANDIDATE_UNIVERSE),
        "endpoints": {
            "scan":    "GET /candidates?limit=10",
            "scan_20": "GET /candidates?limit=20",
            "log":     "GET /candidates/log?last=1",
            "health":  "GET /health",
        },
        # ── 트리니티 연동 가이드 ──────────────────────────────
        "trinity_integration": {
            "status": "수동 연동 (v1.1)",
            "how_to": [
                "1. GET /candidates 호출 → 후보 리스트 확인",
                "2. 관심 종목 code를 트리니티(건눌재/안눌상)에 수동 입력",
                "3. 트리니티가 정밀 진입 판단 수행",
            ],
            "next_version": "v1.2 — Candidate → 건눌재 자동 전달 파이프라인 예정",
            "trinity_url": "https://fastapi-production-f631.up.railway.app",
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# § 8.  엔드포인트: GET /health
# ═══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """헬스체크."""
    kis_ok = bool(KIS_APP_KEY and KIS_APP_SECRET)
    return {
        "status":       "ok",
        "mode":         "real" if KIS_MODE == "real" else "mock",
        "kis_key_set":  kis_ok,
        "universe":     len(CANDIDATE_UNIVERSE),
        "log_stored":   len(_scan_log_store),
        "checked_at":   datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ═══════════════════════════════════════════════════════════════════════
# § 9.  엔드포인트: GET /candidates
# ═══════════════════════════════════════════════════════════════════════

@app.get("/candidates")
async def get_candidates(limit: int = 10):
    """
    1차 후보 스캔.

    ⚠️  이 결과는 1차 필터링용입니다.
        정밀 진입 판단은 트리니티(건눌재/안눌상)에 넣어서 수행하세요.

    Query Params:
      limit: 10 (기본) | 20

    Response:
    {
      "scanner_type": "1차 후보 스캐너",
      "mode": "mock",
      "limit": 10,
      "count": 4,
      "scanned_at": "2026-04-21T09:31:00",
      "trinity_note": "이 결과를 트리니티에 수동으로 넣어 정밀 판단하세요.",
      "candidates": [
        {
          "code": "267250",
          "name": "HD현대일렉트릭",
          "price": 185000,
          "change_rate": 3.5,
          "volume_ratio": 2.1,
          "score": 90,
          "grade": "S",
          "status": "돌파 직전",
          "reason": ["MA20 위 구조", "거래량 급증", "VWAP 위 포지션", "섹터 선도주"],
          "risk": [],
          "score_breakdown": { "structure": 40, "timing": 30, "catalyst": 10, "stability": 10 },
          "data_quality": {
            "overall": "proxy", "price": "exact", "volume": "exact",
            "vwap": "exact", "ma5": "proxy", "ma20": "proxy", "high_20d": "proxy",
            "note": "..."
          }
        }
      ]
    }
    """
    if limit > 20: limit = 20
    elif limit < 10: limit = 10

    candidates, _ = await scan_candidates(limit=limit)

    return {
        "scanner_type": SCANNER_TYPE,
        "scanner_note": SCANNER_NOTE,
        "mode":         "real" if KIS_MODE == "real" else "mock",
        "limit":        limit,
        "count":        len(candidates),
        "scanned_at":   datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # 트리니티 연동 안내 — 항상 표시
        "trinity_note": "이 결과를 트리니티(건눌재/안눌상)에 수동으로 넣어 정밀 판단하세요. (v1.2에서 자동 연동 예정)",
        "candidates":   candidates,
    }


# ═══════════════════════════════════════════════════════════════════════
# § 10. 엔드포인트: GET /candidates/log
# ═══════════════════════════════════════════════════════════════════════

@app.get("/candidates/log")
async def get_scan_log(last: int = 1):
    """
    최근 스캔 복기 로그.

    Query Params:
      last: 최근 N회 (기본 1, 최대 20)

    Response:
    {
      "log_count": 1,
      "logs": [
        {
          "scanned_at": "...",
          "mode": "mock",
          "total_scanned": 30,
          "passed_filter": 12,
          "passed_score": 4,
          "returned": 4,
          "passed": [
            { "code": "267250", "name": "HD현대일렉트릭",
              "score": 90, "grade": "S",
              "breakdown": {"structure":40,"timing":30,"catalyst":10,"stability":10} }
          ],
          "rejected_filter": [
            { "code": "005930", "name": "삼성전자",
              "reject_reasons": ["거래량 감소"] }
          ],
          "rejected_score": [
            { "code": "035720", "name": "카카오",
              "score": 55,
              "breakdown": {"structure":30,"timing":15,"catalyst":0,"stability":10},
              "missing": ["촉매 없음 (0/20)"] }
          ]
        }
      ]
    }
    """
    last = max(1, min(last, 20))
    logs = list(_scan_log_store)[-last:]
    return {
        "log_count": len(logs),
        "logs":      logs,
    }


# ═══════════════════════════════════════════════════════════════════════
# § 11. 로컬 실행 진입점
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("candidate_engine:app", host="0.0.0.0", port=8001, reload=True)
