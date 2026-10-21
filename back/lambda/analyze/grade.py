"""Rule signals (B4), grade combination (B6) and coverage/limitations text for one analysis.

R1~R3 are a provisional reference so the D9 combination rule (report figure 2) can run in this harness.
B4 belongs to 김범수; every threshold below is uncalibrated until the labelled validation set exists.
None of this reads model output except the exceptions, so a rules-only (PARTIAL) result uses the same code.
"""
import re
from collections import Counter, defaultdict

# Thresholds were set on 2026-10-21 against a provisional label set: 210 reviews from the 5 products, labelled
# independently by two LLM labellers (kappa 0.91 mismatch, 0.87 template), disagreements dropped. Positives
# are few (R2 5, R3 7, R1 3 pairs), so treat these as starting values until the team's labelled set exists.
R1_MIN_CHARS = 20        # shorter bodies ("좋아요") match each other trivially
R1_JACCARD = 0.5         # spec draft said 0.8; all 3 labelled copy pairs were 0.54~0.97, no pair sat in 0.4~0.5
R3_STOCK_MIN_CHARS = 20  # a repeated sentence must be at least this long (spaces/punctuation removed)
R3_STOCK_REVIEWS = 3     # ... and appear in this many different reviews
R3_LOW_INFO_CHARS = 4    # bodies this short carry almost no information ("좋아요")
R2_MAX_CHARS = 60        # labelled R2 cases were short defect notes under a high rating ("흠집 있음" ★4)

# R2: a short review whose words point the other way from its rating. A whole-body sentiment lexicon scored
# precision 0.03 on the labels (long reviews list both sides), so only short bodies are judged.
DEFECT = re.compile(r"기스|흠집|스크래치|불량|고장|더블\s?클릭|안\s?됨|안\s?돼요|안\s?되요|빠짐|빠졌|파손|얼룩|재포장|반품|환불|최악|비추|쓰레기|후회|실망")
PRAISE = re.compile(r"강추|최고|만족|추천|좋아요|좋습니다|좋네요|좋은|괜찮|편해|편하|예뻐|이뻐|짱|굿")

LEVEL_WORD = {"HIGH": "믿을 만함", "MEDIUM": "보통", "LOW": "주의", "INSUFFICIENT": "판단하기 어려움"}


def plain(text):
    return re.sub(r"[\W_]+", "", text)


def grams(text):
    return {text[i:i + 3] for i in range(len(text) - 2)}


def rule_signals(index):
    """{rid: set of 'R1'|'R2'|'R3'} from review text, rating and dates only."""
    signals = defaultdict(set)
    bodies = {it["rid"]: plain(it["review"]["body"]) for it in index}

    long = [(rid, grams(b)) for rid, b in bodies.items() if len(b) >= R1_MIN_CHARS]
    for i, (a, ga) in enumerate(long):
        for b, gb in long[i + 1:]:
            if len(ga & gb) >= R1_JACCARD * len(ga | gb):
                signals[a].add("R1"); signals[b].add("R1")

    for it in index:
        rating, body = it["review"].get("rating"), it["review"]["body"]
        if not rating or len(bodies[it["rid"]]) > R2_MAX_CHARS:
            continue
        defect, praise = DEFECT.search(body), PRAISE.search(body)
        if (rating >= 4 and defect and not praise) or (rating <= 2 and praise and not defect):
            signals[it["rid"]].add("R2")

    owners = defaultdict(set)
    for it in index:
        for s in it["sentences"]:
            if len(plain(s)) >= R3_STOCK_MIN_CHARS:
                owners[plain(s)].add(it["rid"])
    for rids in owners.values():
        if len(rids) >= R3_STOCK_REVIEWS:
            for rid in rids:
                signals[rid].add("R3")
    for rid, b in bodies.items():
        if len(b) <= R3_LOW_INFO_CHARS:
            signals[rid].add("R3")
    return signals


def review_level(codes, exception_type):
    """D9, first matching line wins."""
    if "R1" in codes or exception_type == "unrelated":
        return "LOW"
    if codes == {"R2"} and exception_type == "irony":
        return "HIGH"
    if codes & {"R2", "R3"}:
        return "MEDIUM"
    return "HIGH"


def combine(index, signals, exceptions, sponsored, reward_count):
    """Spec table 8 `reliability`. exceptions: [{review_id: 'R<n>', type}] in harness IDs."""
    ex = {e["review_id"]: e["type"] for e in exceptions}
    by_rid = {it["rid"]: it["review"]["review_id"] for it in index}
    levels = {it["rid"]: review_level(signals.get(it["rid"], set()), ex.get(it["rid"])) for it in index}
    counts = Counter(levels.values())
    n = len(index)
    low = counts["LOW"]
    if n < 5:
        level = "INSUFFICIENT"
    elif low >= 0.3 * n:
        level = "LOW"
    elif low >= 0.1 * n:
        level = "MEDIUM"
    else:
        level = "HIGH"

    sig = Counter(code for codes in signals.values() for code in codes)
    unrelated = sum(1 for t in ex.values() if t == "unrelated")
    parts = [f"분석한 리뷰 {n}건을 봤어요."]
    if level == "INSUFFICIENT":
        parts.append("리뷰가 너무 적어서 믿을 만한지 판단하기 어려워요.")
    else:
        found = []
        if sig["R1"]:
            found.append(f"서로 거의 같은 글로 쓴 리뷰 {sig['R1']}건")
        if unrelated:
            found.append(f"상품과 관계없는 리뷰 {unrelated}건")
        parts.append((", ".join(found) + "이 있어요." if found else "복사한 듯 같은 글이나 상품과 관계없는 리뷰는 없었어요."))
        parts.append("이런 리뷰가 열에 한 건 이상이면 '보통', 열에 세 건 이상이면 '주의'로 봐요.")
    if sponsored:
        parts.append(f"협찬·체험단 표시가 있는 리뷰 {sponsored}건은 처음부터 분석에서 뺐어요.")
    if reward_count:
        parts.append(f"리뷰를 쓰면 보상을 준다는 언급이 {reward_count}건 있어요. 좋은 리뷰가 부풀려졌을 수 있어요.")

    reviews = [{"review_id": by_rid[rid], "level": lv,
                "codes": sorted(signals.get(rid, set()) | ({f"AI_{ex[rid].upper()}"} if rid in ex else set()))}
               for rid, lv in levels.items() if lv != "HIGH"]
    return {"level": level, "level_word": LEVEL_WORD[level], "reason": " ".join(parts),
            "level_counts": {k: counts[k] for k in ("HIGH", "MEDIUM", "LOW")},
            "signals": {"R1": sig["R1"], "R2": sig["R2"], "R3": sig["R3"], "AI_UNRELATED": unrelated,
                        "AI_IRONY": sum(1 for t in ex.values() if t == "irony")},
            "reviews": reviews}


EXCLUDED = (("sponsored", "협찬·체험단 표시"), ("empty", "내용 없는 리뷰"), ("duplicate", "똑같은 리뷰"))


def coverage(data, index):
    """Spec table 8 `coverage` and plain-language `limitations` from the collection record."""
    by = sorted(data.get("collection", {}).get("by_rating", []), key=lambda r: -r["rating"])
    excluded = Counter()
    for r in by:
        excluded.update({k: v for k, v in r["excluded"].items() if v and isinstance(v, int)})
    failed = data.get("collection", {}).get("failed_ratings") or []
    lim = []
    totals = [(r["rating"], r["total"]) for r in by if r.get("total") is not None]
    if totals:  # the denominator, when the collector could read it from the rating filter
        lim.append("쿠팡에 올라온 리뷰: " + ", ".join(f"{k}점 {v:,}건" for k, v in totals)
                   + ". 이 중 별점마다 최대 100건을 읽었어요.")
    if excluded["over_limit"]:
        per = ", ".join(f"{r['rating']}점 {r['collected']}건" for r in by)
        lim.append(f"글자 수 합계 상한 때문에 별점마다 비슷한 수만 남기고 {excluded['over_limit']}건은 분석하지 않았어요({per}). "
                   "그래서 별점별 리뷰 비율은 쿠팡 실제 비율과 달라요.")
    dropped = [f"{label} {excluded[k]}건" for k, label in EXCLUDED if excluded[k]]
    if dropped:
        lim.append("분석에서 뺀 리뷰: " + ", ".join(dropped) + ". 빼기만 했고 가짜라고 판정한 것은 아니에요.")
    dates = defaultdict(list)
    for it in index:
        r = it["review"]
        if r.get("rating") and r.get("written_at"):
            dates[r["rating"]].append(r["written_at"][:7])
    if dates:
        lim.append("분석한 리뷰를 쓴 시기: " + ", ".join(
            f"{k}점 {min(v)}~{max(v)}" for k, v in sorted(dates.items(), reverse=True)) + ".")
    no_rating = sum(1 for it in index if not it["review"].get("rating"))
    no_date = sum(1 for it in index if not it["review"].get("written_at"))
    if no_rating or no_date:
        lim.append(f"별점이 없는 리뷰 {no_rating}건, 작성일이 없는 리뷰 {no_date}건이 있어요.")
    if failed:
        lim.append(f"{', '.join(f'{x}점' for x in failed)} 리뷰는 가져오지 못해 빠졌어요.")
    if len(index) < 30:
        lim.append(f"분석한 리뷰가 {len(index)}건뿐이라 결과가 치우칠 수 있어요.")
    cov = {"received": sum(r["collected"] + sum(r["excluded"].values()) for r in by),
           "analyzed": len(index), "by_rating": by, "failed_ratings": failed, "excluded": dict(excluded)}
    return cov, lim
