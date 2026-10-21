"""Local prompt harness: collected Coupang review JSON -> Claude (subscription, via Claude Code) -> checked result.

Map-reduce, because one call over ~500 reviews mis-links evidence (NOTES.md):
  map    : each batch of BATCH reviews -> opinion points citing review/sentence IDs (prompt_map.md)
  reduce : all points -> grouped pros/cons/key_opinions that only reference point IDs (prompt_reduce.md)
  verify : every candidate quote and exception is checked against the review text (prompt_verify.md)
  code   : support_count, evidence quotes, exceptions, rule signals and the grade come from IDs and rules (grade.py).

Usage:
  python3 run.py <reviews.json> [--model claude-haiku-4-5-20251001]
  python3 run.py --selftest

Everything except call_claude() is meant to move into the analyze Lambda; call_claude() becomes a
Bedrock Converse call (same prompts, schema as the forced tool's inputSchema).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # grade.py sits next to this file; -I drops the script dir
import grade  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # Bedrock target is Haiku 4.5; the "haiku" alias resolves to a newer model
BATCH = 50  # 50 reviews per call cited correctly in the 2026-10-20 test; 213 did not
PARALLEL = 4


# A line that stops mid-sentence: no closing punctuation and no usual sentence ending. Reviews typed on
# phones wrap like this ("2개중에 한개 제품은\n누가 개봉한건지"), and v3 quoted the cut halves.
# ponytail: ending heuristic, misses odd endings; a Korean sentence splitter (kss) if quotes still break.
OPEN_LINE = re.compile(r"(?:[,，]|(?<![다요음임함됨죠네까듯ㅋㅎㅠㅜ!?.~)\]…^*:]))$")
TAIL = re.compile(r"^(?:[.,)\]][^\s]{0,15}|\([^)]{0,12}\)[.!?~]*)$")
MAX_JOINED = 150  # stop joining past this; one 685-char "sentence" in v5 mixed three topics into one quote
BULLET = re.compile(r"^(?:[-*•·✓✔✅☑❤▶➡✨✖❌⭐#>+]|\d+[.)]|[①-⑳])")


def split_sentences(body):
    """Lines (rejoining wrapped ones), then sentence ends. Citing by sentence ID keeps the model from rewriting quotes."""
    lines = []
    for line in body.split("\n"):
        line = line.strip()
        if lines and lines[-1] and TAIL.match(line):  # ".(갤20)" on its own line belongs to the line above
            lines[-1] += line
        elif (lines and lines[-1] and line and OPEN_LINE.search(lines[-1]) and not BULLET.match(line)
                and not BULLET.match(lines[-1]) and len(lines[-1]) + len(line) <= MAX_JOINED):
            lines[-1] += " " + line
        else:
            lines.append(line)
    parts = []
    for line in lines:
        parts += [p.strip() for p in re.split(r"(?<=[.!?~])\s+", line) if p.strip()]
    return parts


def build_index(data):
    """Short IDs for the whole request: R<n> -> original review, R<n>.<k> -> sentence text."""
    reviews = []
    for n, r in enumerate(data["reviews"], 1):
        reviews.append({"rid": f"R{n}", "review": r, "sentences": split_sentences(r["body"])})
    return reviews


def build_map_message(product, batch):
    lines = [f"상품명: {product.get('title') or '-'}", f"이 묶음의 리뷰 수: {len(batch)}", "", "리뷰 목록:"]
    for item in batch:
        r = item["review"]
        rating = f"★{r['rating']}" if r.get("rating") else "★-"
        lines.append(f"\n{item['rid']} {rating} | {r.get('written_at') or '-'} | {r.get('option') or '-'}")
        lines += [f"{item['rid']}.{k} {s}" for k, s in enumerate(item["sentences"], 1)]
    return "\n".join(lines)


QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{2,})['\"‘’“”]")


def squash(text):
    """Compare quotes ignoring spaces and punctuation, so '좋아요~' still matches '좋아요!!'."""
    return re.sub(r"[\W_]+", "", text)


def check_map(raw, batch, batch_no):
    """Keep only IDs that exist in this batch. Returns (points keyed B<b>-P<p>, exceptions, problems).

    An unrelated exception whose review the model also cited in points has product content, so the
    points win. A reason that quotes words missing from that review is dropped (v3 copied a
    neighbouring review's words into one)."""
    rids = {item["rid"]: item for item in batch}
    problems, exceptions = [], []
    cited = {rid for p in raw["points"] for rid in p["reviews"]}
    for e in raw["exceptions"]:
        rid = e["review_id"]
        if rid not in rids:
            problems.append(f"B{batch_no} exception {rid}: not in batch")
        elif e.get("type") not in ("irony", "unrelated"):
            problems.append(f"B{batch_no} exception {rid}: unknown type {e.get('type')!r}, dropped")  # Bedrock ignores enums
        elif e["type"] == "unrelated" and rid in cited:
            problems.append(f"B{batch_no} exception {rid}: unrelated but cited in points, kept as review")
        elif any(squash(q) not in squash(rids[rid]["review"]["body"]) for q in QUOTED.findall(e["reason"])):
            problems.append(f"B{batch_no} exception {rid}: reason quotes text not in the review, dropped")
        else:
            exceptions.append(e)
    # unrelated reviews leave the counts; irony reviews count with their real meaning but are never quoted,
    # because their literal words say the opposite (884923917 "쿠팡에서 중고거래를 했습니다 ㅋㅋ").
    excluded = {e["review_id"] for e in exceptions if e["type"] == "unrelated"}
    unquotable = {e["review_id"] for e in exceptions}
    points = {}
    for p_no, p in enumerate(raw["points"], 1):
        pid = f"B{batch_no}-P{p_no}"
        reviews = []
        for rid in dict.fromkeys(p["reviews"]):
            if rid not in rids:
                problems.append(f"{pid} review {rid}: not in batch")
            elif rid not in excluded:
                reviews.append(rid)
        evidence = []
        for sid in p["evidence"]:
            rid, _, k = sid.partition(".")
            item = rids.get(rid)
            if not item or not (1 <= int(k) <= len(item["sentences"])):
                problems.append(f"{pid} evidence {sid}: unknown sentence")
            elif rid in unquotable:
                problems.append(f"{pid} evidence {sid}: cites an exception review")
            elif rid not in reviews:
                problems.append(f"{pid} evidence {sid}: review not listed as support")
            else:
                evidence.append(sid)
        if reviews:
            points[pid] = {**p, "reviews": reviews, "evidence": evidence}
    return points, exceptions, problems


def build_reduce_message(product, points, index):
    """Each point carries its evidence sentences, so reduce groups and picks quotes by what reviews actually say."""
    by_rid = {item["rid"]: item for item in index}
    lines = [f"상품명: {product.get('title') or '-'}", f"의견 수: {len(points)}", "", "의견 목록:"]
    for pid, p in points.items():
        cond = ", ".join(p["conditions"]) or "-"
        lines.append(f"{pid} [{p['kind']}/{p['stance']}] {p['aspect']} | {p['statement']} | {cond} | 리뷰 {len(p['reviews'])}건")
        for sid in p["evidence"]:
            rid, _, k = sid.partition(".")
            lines.append(f"  {sid} \"{by_rid[rid]['sentences'][int(k) - 1]}\"")
    return "\n".join(lines)


def guard_aspect(aspect, members, points):
    """Keep the model's group name only if member points use it; v4 invented '호합성' for 호환성."""
    if any(grade.plain(aspect) in grade.plain(points[m]["aspect"] + points[m]["statement"]) for m in members):
        return aspect
    return Counter(points[m]["aspect"] for m in members).most_common(1)[0][0]


SECTIONS = ("pros", "cons", "key_opinions")
# Shipping-only text is not evidence about the product (persona round 1; came back in round 3 runs).
PACKAGING = re.compile(r"박스|상자|완충[재제]|뽁뽁|택배|봉투|에어캡|포장|배송")
ITEM_WORDS = re.compile(r"재포장|중고|사용\s?흔적|반품|반송|정품|짝퉁|가품|시리얼|기스|스크래치|흠집|불량|고장|먼지|손때|개봉|뜯은|뜯겨|구성품")


def shipping_only(text):
    return bool(PACKAGING.search(text)) and not ITEM_WORDS.search(text)
EXPECTED_SIDE = {"pros": "positive", "cons": "negative"}
MAX_CANDIDATES = 6  # quotes per card sent to the verify call; keeps that call small
MAX_QUOTE = 150


def sentence(sid, by_rid):
    rid, _, k = sid.partition(".")
    return by_rid[rid]["sentences"][int(k) - 1]


def cards(raw):
    """Card IDs C1.. over pros, cons, key_opinions in reduce order, shared by verify and assemble."""
    out = []
    for section in SECTIONS:
        for g in raw[section]:
            out.append((f"C{len(out) + 1}", section, g))
    return out


def candidates(g, points, by_rid):
    """Reduce's picks first, then the other member evidence (largest member points first), at most MAX_CANDIDATES.
    Older results without reduce picks use member evidence only."""
    members = [m for m in g["members"] if m in points]
    owner = {sid: m for m in members for sid in points[m]["evidence"]}
    picks = [sid for sid in g.get("evidence", []) if sid in owner]
    rest = [sid for m in sorted(members, key=lambda m: -len(points[m]["reviews"])) for sid in points[m]["evidence"]]
    ordered = list(dict.fromkeys(picks + rest))
    ordered.sort(key=lambda sid: len(sentence(sid, by_rid)) > MAX_QUOTE)  # long sentences mix topics; last
    return ordered[:MAX_CANDIDATES], owner


def assemble(raw, points, index, problems, flagged=frozenset(), verdicts=None, card_verdicts=None):
    """Spec table 8 shape. support_count = distinct reviews across member points.

    Quotes come from candidates(). With verdicts (from the verify call) a quote must fit the card and sit on
    the card's side; a card left without a fitting quote is dropped, and a mixed card needs both sides.
    At most 3 quotes from different reviews; a review quoted by an earlier card is skipped unless nothing
    else is left, and reviews with R1/R3 rule signals (copied or template text) go to the back."""
    by_rid = {item["rid"]: item for item in index}
    used = set()  # reviews already quoted anywhere in this result

    def side_of(card, sid, owner):
        v = (verdicts or {}).get((card, sid))
        return v["side"] if v else points[owner[sid]]["stance"]

    def pick(card, section, g):
        members = [m for m in dict.fromkeys(g["members"]) if m in points]
        problems.extend(f"{section} member {m}: unknown point" for m in g["members"] if m not in points)
        if not members:
            return members, 0, []
        reviews = {rid for m in members for rid in points[m]["reviews"]}
        cands, owner = candidates(g, points, by_rid)
        problems.extend(f"{card} {section} evidence {sid}: not a member's evidence"
                        for sid in g.get("evidence", []) if sid not in owner)
        if verdicts is not None:
            want = EXPECTED_SIDE.get(section) or (g["stance"] if g["stance"] != "mixed" else None)
            ok = []
            for sid in cands:
                v = verdicts.get((card, sid))
                if v and v["fits"] and (want is None or v["side"] in (want, "mixed")):
                    ok.append(sid)
                else:
                    problems.append(f"{card} {section} evidence {sid}: {'not verified' if not v else 'does not fit'}, dropped")
            cands = ok
        if section != "pros":
            dropped = [sid for sid in cands if shipping_only(sentence(sid, by_rid))]
            problems.extend(f"{card} {section} evidence {sid}: shipping only, dropped" for sid in dropped)
            cands = [sid for sid in cands if sid not in dropped]
        fresh = [sid for sid in cands if sid.partition(".")[0] not in used]
        fresh.sort(key=lambda sid: sid.partition(".")[0] in flagged)
        pool = fresh or cands[:1]
        if section == "key_opinions" and g["stance"] == "mixed":
            # one quote per side first, so a "갈림" card shows both sides
            firsts = [next((sid for sid in pool if side_of(card, sid, owner) == s), None) for s in ("positive", "negative")]
            pool = [sid for sid in firsts if sid] + pool
        chosen, seen = [], set()
        for sid in pool:
            rid = sid.partition(".")[0]
            if rid not in seen and len(chosen) < 3:
                chosen.append(sid)
                seen.add(rid)
        used.update(seen)
        evidence = []
        for sid in chosen:
            rid, _, k = sid.partition(".")
            evidence.append({"review_id": by_rid[rid]["review"]["review_id"], "quote": by_rid[rid]["sentences"][int(k) - 1],
                             "stance": side_of(card, sid, owner)})
        return members, len(reviews), evidence

    result = {s: [] for s in SECTIONS}
    groups, names = [], set()
    for card, section, g in cards(raw):
        members = [m for m in g["members"] if m in points]
        title = guard_aspect(g.get("aspect") or g.get("text", ""), members, points) if members else g.get("aspect")
        if section != "pros" and title and shipping_only(f"{title} {g.get('statement') or g.get('text') or ''}"):
            problems.append(f"{card} {section} {title}: about shipping, not the product, dropped")
            continue
        if section == "key_opinions":
            if any(set(g["members"]) <= grp for grp in groups):
                problems.append(f"{card} key_opinions {title}: repeats one pros/cons group, dropped")
                continue
            if title and grade.plain(title) in names:
                problems.append(f"{card} key_opinions {title}: same aspect as a pros/cons card, dropped")
                continue
        members, count, evidence = pick(card, section, g)
        if not members:
            continue
        cv = (card_verdicts or {}).get(card, {})
        if cv and not cv["statement_ok"]:
            # An overreaching summary ("3~6개월 후 고장이 많다" from one review, persona P2 round 4) falls back to
            # the largest member opinion's own sentence, which map wrote from a single batch of reviews.
            core = max(members, key=lambda m: len(points[m]["reviews"]))
            problems.append(f"{card} {section} {title}: summary overreaches, replaced by member {core}")
            g = {**g, ("text" if section == "key_opinions" else "statement"): points[core]["statement"]}
        if cv and not cv["conditions_ok"] and g.get("conditions"):
            problems.append(f"{card} {section} {title}: conditions contradict quotes, dropped")
            g = {**g, "conditions": []}
        if verdicts is not None and not evidence:
            problems.append(f"{card} {section} {title}: no quote fits, dropped")
            continue
        sides = {e["stance"] for e in evidence}
        if section == "key_opinions" and g["stance"] == "mixed" and "mixed" not in sides and not {"positive", "negative"} <= sides:
            if verdicts is not None:
                problems.append(f"{card} key_opinions {title}: mixed but quotes are one-sided, dropped")
                continue
            problems.append(f"{card} key_opinions {title}: mixed but evidence is one-sided")
        if section == "key_opinions":
            result[section].append({"aspect": title, "text": g["text"], "stance": g["stance"],
                                    "support_count": count, "evidence": evidence, "members": members})
        else:
            groups.append(set(members))
            names.add(grade.plain(title))
            result[section].append({"aspect": title, "statement": g["statement"], "support_count": count,
                                    "conditions": g["conditions"], "evidence": evidence, "members": members})
    for section in ("pros", "cons"):
        result[section].sort(key=lambda r: -r["support_count"])
    return result


def build_verify_message(product, raw, points, index, exceptions):
    by_rid = {item["rid"]: item for item in index}
    lines = [f"상품명: {product.get('title') or '-'}", "", "카드 목록:"]
    for card, section, g in cards(raw):
        kind = {"pros": "pro", "cons": "con", "key_opinions": "key"}[section]
        stance = g.get("stance") or EXPECTED_SIDE[section]
        cond = f" | 조건: {', '.join(g['conditions'])}" if g.get("conditions") else ""
        lines.append(f"\n{card} [{kind}/{stance}] {g.get('aspect') or '-'} | {g.get('statement') or g.get('text')}{cond}")
        for sid in candidates(g, points, by_rid)[0]:
            lines.append(f"  {sid} \"{sentence(sid, by_rid)}\"")
    lines += ["", "예외 리뷰 목록:"]
    for e in exceptions:
        item = by_rid[e["review_id"]]
        lines.append(f"\n{e['review_id']} [{e['type']}] ★{item['review'].get('rating') or '-'}")
        lines += [f"{e['review_id']}.{k} {t}" for k, t in enumerate(item["sentences"][:MAX_EXCEPTION_SENTENCES], 1)]
    return "\n".join(lines)


MAX_EXCEPTION_SENTENCES = 20
# A review that says it was edited ("라고 썼었는데 수정합니다") changed its verdict; that is not irony (prompt rule v3 ignored).
EDITED = re.compile(r"수정\s*(?:합니다|해요|함|했|할게)|\(수정\)|후기\s*수정|썼었는데|추가\s*(?:후기|합니다)")


# Words for the state of the item itself (scratches, signs of use, resale). A review using them is about the
# product, whatever else it complains about; the verifier still missed "박스파손 제품기스" (160783113) in v5.
# Every v3~v5 unrelated flag containing one of these was a product-content review (checked by hand).
PRODUCT_STATE = re.compile(r"기스|흠집|스크래치|스크라치|사용\s?흔적|사용감|중고|재포장|짝퉁|가품|찍힘|얼룩|손때")


# Breakdown words. In a 1~2-star review they describe this item failing; 5-star reviews use them for the old
# item that was replaced, so the rule is limited to low ratings (no false hits among 64 past low-rated flags).
FAILURE = re.compile(r"작동\s?(?:이|을|도)?\s?(?:안|않|꺼|멈|불량)|만\s?작동|꺼져|꺼짐|고장|접촉\s?불량|불량품|연결이?\s?안|"
                     r"클릭\s?(?:조차|이|도)?\s?안|충전이?\s?안|안\s?켜|구성품|끊기|끊겨|끊김|부품이?\s?(?:빠|없)|파손")


def check_exceptions(exceptions, index, verify_raw, problems):
    """Drop an unrelated flag when the verifier names product sentences of that review, and an irony flag
    when the review says it was edited or the verifier says it is not irony."""
    by_rid = {item["rid"]: item for item in index}
    judged = {v["review_id"]: v for v in (verify_raw or {}).get("exceptions", [])}
    kept = []
    for e in exceptions:
        rid, item = e["review_id"], by_rid[e["review_id"]]
        v = judged.get(rid)
        product = [s for s in (v or {}).get("product_sentences", [])
                   if s.partition(".")[0] == rid and 1 <= int(s.partition(".")[2]) <= len(item["sentences"])]
        if e["type"] == "irony" and EDITED.search(item["review"]["body"]):
            problems.append(f"exception {rid}: edited review, not irony")
        elif e["type"] == "irony" and not (v and v["irony"]):
            # All four irony flags in the second persona round were plain complaints (294927829 "에이 별로야").
            problems.append(f"exception {rid}: verifier did not confirm irony")
        elif e["type"] == "unrelated" and product:
            problems.append(f"exception {rid}: has product sentences {product}, not unrelated")
        elif e["type"] == "unrelated" and PRODUCT_STATE.search(item["review"]["body"]):
            problems.append(f"exception {rid}: describes the item's condition, not unrelated")
        elif e["type"] == "unrelated" and (item["review"].get("rating") or 5) <= 2 and FAILURE.search(item["review"]["body"]):
            problems.append(f"exception {rid}: low rating describing a failure, not unrelated")
        else:
            kept.append(e)
    return kept


# Mentions of rewards for writing reviews (payback, gift cards, review events). Counted by rule over every
# review, because the model folded v3's three payback reviews into one opinion of 1 review.
REWARD = re.compile(r"페이백|상품권|적립금|리뷰\s*(?:이벤트|조작|작성\s*시|쓰면|써\s*주면|좋게)|5\s*점\s*(?:리뷰|주면|달라)")


def reward_signal(index):
    """Spec table 8 reliability.signals entry: reviews mentioning review rewards, with the matching sentence."""
    hits = []
    for item in index:
        for s in item["sentences"]:
            if REWARD.search(s):
                hits.append({"review_id": item["review"]["review_id"], "quote": s})
                break
    return {"count": len(hits), "evidence": hits}


def finish(data, index, points, reduce_raw, verify_raw, exceptions, problems):
    """Everything after the model calls: exception checks, verified quotes, rule signals, grade, coverage.
    Also used to rebuild a saved result offline from its raw model outputs."""
    signals = grade.rule_signals(index)
    flagged = {rid for rid, codes in signals.items() if codes & {"R1", "R3"}}
    exceptions = check_exceptions(exceptions, index, verify_raw, problems)
    verdicts = None
    if verify_raw is not None:
        verdicts = {(v["card"], v["sentence"]): v for v in verify_raw["evidence"]}
    card_verdicts = {v["card"]: v for v in (verify_raw or {}).get("cards", [])}
    analysis = assemble(reduce_raw, points, index, problems, flagged, verdicts, card_verdicts)
    by_rid = {item["rid"]: item["review"]["review_id"] for item in index}
    analysis["exceptions"] = [{**e, "review_id": by_rid[e["review_id"]]} for e in exceptions]
    sponsored = sum(r["excluded"].get("sponsored", 0) for r in data.get("collection", {}).get("by_rating", []))
    reward = reward_signal(index)
    analysis["reliability"] = grade.combine(index, signals, exceptions, sponsored, reward["count"], data.get("product", {}).get("title", ""))
    analysis["reliability"]["signals"]["reward_mention"] = reward
    analysis["coverage"], analysis["limitations"] = grade.coverage(data, index)
    return analysis


def call_claude(system_prompt, user_message, schema, model, temperature=None):  # temperature: Bedrock only
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",               # no tools: plain completion, like Converse
        "--setting-sources", "",     # keep personal CLAUDE.md, hooks and plugins out of the prompt
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
    ]
    proc = subprocess.run(cmd, input=user_message, capture_output=True, text=True, timeout=600, cwd=HERE)
    if proc.returncode != 0:
        raise RuntimeError(f"claude failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or "structured_output" not in out:
        raise RuntimeError(f"claude returned no structured output: {out.get('result')}")
    return out


def coerce(value, schema):
    """Undo one Converse quirk: a nested array or object sometimes arrives as a JSON string."""
    t = schema.get("type")
    if isinstance(value, str) and t in ("array", "object"):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if t == "object" and isinstance(value, dict):
        props = schema.get("properties", {})
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and props.get(k, {}).get("type") in ("array", "object"):
                try:
                    json.loads(v)
                except ValueError:
                    # Seen on Bedrock: every remaining key written inside the first one's string,
                    # '[...], "cons": [...], "key_opinions": [...]'. Wrapping it back recovers all keys.
                    fixed = re.sub(r'"(\w+)">\s*(?=[\[{])', r'"\1": ', v)  # also seen: "cons">[ for "cons": [
                    for text in (v, fixed, fixed.rstrip().removesuffix("}")):  # and the object's own closing brace inside
                        try:
                            out.update(json.loads(f'{{"{k}": {text}}}'))
                            break
                        except ValueError:
                            continue
                    else:
                        out[k] = v
                    continue
            out[k] = v
        return {k: coerce(v, props[k]) if k in props else v for k, v in out.items()}
    if t == "array" and isinstance(value, list):
        return [coerce(v, schema.get("items", {})) for v in value]
    return value


def schema_gaps(value, schema, path="$"):
    """Required keys and array/object shapes missing from a tool input (a small subset of JSON Schema)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: not an object"]
        gaps = [f"{path}.{k}: missing" for k in schema.get("required", []) if k not in value]
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                gaps += schema_gaps(value[k], sub, f"{path}.{k}")
        return gaps
    if t == "array":
        if not isinstance(value, list):
            return [f"{path}: not an array"]
        return [g for i, item in enumerate(value) for g in schema_gaps(item, schema.get("items", {}), f"{path}[{i}]")]
    return []


BEDROCK_ATTEMPTS = 3


def call_bedrock(system_prompt, user_message, schema, model, attempt=1, temperature=0):
    """Same contract as call_claude() over Bedrock Converse (temperature 0, forced tool for structured output).
    Used by the analyze Lambda and by --bedrock. Converse does not enforce the tool schema, so the input is
    repaired (coerce) and checked (schema_gaps), with up to BEDROCK_ATTEMPTS tries for bad output or
    transient errors. Returns call_claude()'s keys plus latency_ms, wall_seconds and stop_reason."""
    import boto3  # only needed for --bedrock
    from botocore.config import Config
    started = time.time()
    client = boto3.client("bedrock-runtime", config=Config(read_timeout=300, retries={"max_attempts": 3, "mode": "standard"}))
    try:
        resp = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            # A retry at temperature 0 repeats the same broken output, so later tries sample a little.
            inferenceConfig={"maxTokens": BEDROCK_MAX_TOKENS, "temperature": temperature if attempt == 1 else max(temperature, 0.3)},
            toolConfig={"tools": [{"toolSpec": {"name": "result", "inputSchema": {"json": schema}}}],
                        "toolChoice": {"tool": {"name": "result"}}},
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        transient = code in ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
                             "InternalServerException") or "use case details" in str(e)  # access still propagating
        if transient and attempt < BEDROCK_ATTEMPTS:
            time.sleep(5 * attempt)
            return call_bedrock(system_prompt, user_message, schema, model, attempt + 1, temperature)
        raise
    blocks = [coerce(c["toolUse"]["input"], schema) for c in resp["output"]["message"]["content"] if "toolUse" in c]
    missing = schema_gaps(blocks[0], schema) if blocks else ["no tool call"]
    if missing and os.environ.get("TRUSTLENS_DEBUG_DIR"):
        Path(os.environ["TRUSTLENS_DEBUG_DIR"], f"bad-{time.time():.0f}.json").write_text(
            json.dumps(resp["output"]["message"]["content"], ensure_ascii=False)[:200000], encoding="utf-8")
    if missing:
        # Converse does not enforce the tool schema; v5 on Bedrock got inputs without "points" or "cons",
        # and a reduce call that returned only "pros" three times. The retry names what was missing.
        if attempt < BEDROCK_ATTEMPTS:
            keys = ", ".join(schema.get("required", []))
            reminder = f"\n\n(다시 요청: 앞선 출력에서 {'; '.join(missing[:3])}. 최상위 키 {keys}를 모두 채운 하나의 JSON으로 답한다.)"
            return call_bedrock(system_prompt, user_message.split("\n\n(다시 요청:")[0] + reminder, schema, model, attempt + 1, temperature)
        raise RuntimeError(f"bedrock output does not match schema after {attempt} tries: {missing[:3]} (stopReason={resp.get('stopReason')})")
    usage = resp.get("usage", {})
    return {"structured_output": blocks[0], "usage": {"output_tokens": usage.get("outputTokens"), "input_tokens": usage.get("inputTokens")},
            "total_cost_usd": None, "latency_ms": resp.get("metrics", {}).get("latencyMs"),
            "wall_seconds": round(time.time() - started, 1), "stop_reason": resp.get("stopReason")}


BEDROCK_MAX_TOKENS = 16000  # spec 6.2.6 said 4,000; 50-review map batches hit 4,000 (earphone) and 8,000 (mouse) on 2026-10-21


def load(name):
    text = (HERE / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


VOTES = (0, 0.4, 0.4)  # verify temperatures; a single verify pass flipped between runs (2026-10-21 round 3)


def verify(product, reduce_raw, points, index, exceptions, call, model, verify_model=None):
    """Card quotes and exception reviews are checked in separate calls (asked together, Bedrock Haiku missed
    product sentences in exception reviews), each by three votes at once; a verdict needs two of three."""
    empty = {"pros": [], "cons": [], "key_opinions": []}
    jobs = [("cards", build_verify_message(product, reduce_raw, points, index, []))]
    if exceptions:
        jobs.append(("exceptions", build_verify_message(product, empty, points, index, exceptions)))
    prompt, schema = load("prompt_verify.md"), load("schema_verify.json")
    # A stronger verify model needs one vote: Sonnet 4.5 caught the contrast-clause quote Haiku passed 2 of 3 times.
    votes = (0,) if verify_model else VOTES
    tasks = [(kind, msg, t) for kind, msg in jobs for t in votes]
    with ThreadPoolExecutor(len(tasks)) as pool:
        outs = list(pool.map(lambda job: call(prompt, job[1], schema, verify_model or model, temperature=job[2]), tasks))
    card_votes = [o["structured_output"]["evidence"] for (kind, _, _), o in zip(tasks, outs) if kind == "cards"]
    statement_votes = [o["structured_output"].get("cards", []) for (kind, _, _), o in zip(tasks, outs) if kind == "cards"]
    ex_votes = [o["structured_output"]["exceptions"] for (kind, _, _), o in zip(tasks, outs) if kind == "exceptions"]
    return {"evidence": vote_evidence(card_votes), "cards": vote_cards(statement_votes),
            "exceptions": vote_exceptions(ex_votes)}, outs


def vote_cards(votes):
    """A card's statement or conditions are replaced only when most votes say they overreach."""
    need = len(votes) // 2 + 1
    tally = {}
    for vote in votes:
        for v in vote:
            t = tally.setdefault(v["card"], [0, 0])
            t[0] += not v["statement_ok"]
            t[1] += not v["conditions_ok"]
    return [{"card": c, "statement_ok": bad_s < need, "conditions_ok": bad_c < need} for c, (bad_s, bad_c) in tally.items()]


def vote_evidence(votes):
    tally = {}
    for vote in votes:
        for v in vote:
            tally.setdefault((v["card"], v["sentence"]), []).append(v)
    need = len(votes) // 2 + 1
    return [{"card": c, "sentence": sid, "fits": sum(x["fits"] for x in vs) >= need,
             "side": Counter(x["side"] for x in vs).most_common(1)[0][0]} for (c, sid), vs in tally.items()]


def vote_exceptions(votes):
    need = len(votes) // 2 + 1
    merged = {}
    for vote in votes:
        for v in vote:
            m = merged.setdefault(v["review_id"], {"sentences": Counter(), "irony": 0})
            m["sentences"].update(set(v["product_sentences"]))
            m["irony"] += bool(v["irony"])
    return [{"review_id": rid, "product_sentences": [s for s, n in m["sentences"].items() if n >= need],
             "irony": m["irony"] >= need} for rid, m in merged.items()]


def pipeline(data, call, model, parallel=PARALLEL, log=lambda msg: print(msg, file=sys.stderr), verify_model=None):
    """map -> reduce -> verify -> code steps for one Review Schema 1.1 document. Shared by the CLI and the
    analyze Lambda (08_프로토타입/back), so both run exactly this code. Returns the result record."""
    product = data.get("product", {})
    index = build_index(data)
    batches = [index[i:i + BATCH] for i in range(0, len(index), BATCH)]
    log(f"reviews={len(index)} batches={len(batches)} model={model}")
    started = time.time()

    map_prompt, map_schema = load("prompt_map.md"), load("schema_map.json")
    with ThreadPoolExecutor(parallel) as pool:
        map_outs = list(pool.map(lambda b: call(map_prompt, build_map_message(product, b), map_schema, model), batches))
    points, exceptions, problems = {}, [], []
    for b_no, (batch, out) in enumerate(zip(batches, map_outs), 1):
        p, e, pr = check_map(out["structured_output"], batch, b_no)
        points.update(p); exceptions += e; problems += pr
    map_seconds = round(time.time() - started, 1)
    log(f"map done: points={len(points)} ({map_seconds}s)")

    reduce_out = call(load("prompt_reduce.md"), build_reduce_message(product, points, index), load("schema_reduce.json"), model)
    reduce_raw = reduce_out["structured_output"]
    verify_raw, verify_outs = verify(product, reduce_raw, points, index, exceptions, call, model, verify_model)
    analysis = finish(data, index, points, reduce_raw, verify_raw, exceptions, problems)

    outs = map_outs + [reduce_out] + verify_outs
    return {
        "model": model,
        "batch_size": BATCH,
        "seconds": round(time.time() - started, 1),
        "map_seconds": map_seconds,
        "calls": len(outs),
        "cost_usd_equivalent": round(sum(o.get("total_cost_usd") or 0 for o in outs), 4),  # Claude Code only
        "output_tokens": [o.get("usage", {}).get("output_tokens") for o in outs],
        "input_tokens": [o.get("usage", {}).get("input_tokens") for o in outs],  # Bedrock only
        "call_seconds": [o.get("wall_seconds") for o in outs],  # Bedrock only
        "stop_reasons": [o.get("stop_reason") for o in outs],
        "verify_problems": problems,
        "analysis": analysis,
        "map_points": points,
        "map_raw": [o["structured_output"] for o in map_outs],
        "reduce_raw": reduce_raw,
        "verify_raw": verify_raw,
        "exceptions_raw": exceptions,
    }


def rules_only(data, why):
    """Spec 6.2.1 PARTIAL: the model calls failed, so return rule signals, grade and coverage without AI cards."""
    index = build_index(data)
    signals = grade.rule_signals(index)
    sponsored = sum(r["excluded"].get("sponsored", 0) for r in data.get("collection", {}).get("by_rating", []))
    reward = reward_signal(index)
    analysis = {"pros": [], "cons": [], "key_opinions": [], "exceptions": []}
    analysis["reliability"] = grade.combine(index, signals, [], sponsored, reward["count"], data.get("product", {}).get("title", ""))
    analysis["reliability"]["signals"]["reward_mention"] = reward
    analysis["coverage"], analysis["limitations"] = grade.coverage(data, index)
    analysis["limitations"].insert(0, "AI 요약이 실패해서 장점·단점·핵심 의견 없이 규칙으로 센 결과만 보여 드려요. "
                                      "장단점이 비어 있는 것은 단점이 없다는 뜻이 아니에요.")
    return {"analysis": analysis, "verify_problems": [f"rules only: {why}"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reviews", nargs="?")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--parallel", type=int, default=PARALLEL, help="map calls at once; lower it when memory is short")
    ap.add_argument("--bedrock", metavar="MODEL_ID", help="call Bedrock Converse instead of Claude Code, e.g. global.anthropic.claude-haiku-4-5-20251001-v1:0")
    ap.add_argument("--verify-model", metavar="MODEL_ID", help="one verify vote with this model instead of three with --bedrock's")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--reverify", metavar="RESULT_JSON", help="rerun only the verify call and code steps on a saved result")
    ap.add_argument("--rebuild", metavar="RESULT_JSON", help="rerun only the code steps (no model calls) on a saved result")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    global call_claude
    if args.bedrock:
        call_claude, args.model = call_bedrock, args.bedrock
    if args.reverify or args.rebuild:
        return reverify(Path(args.reverify or args.rebuild), args.model, call=bool(args.reverify))
    if not args.reviews:
        ap.error("reviews.json is required")

    data = json.loads(Path(args.reviews).read_text(encoding="utf-8"))
    record = {"input": str(Path(args.reviews).resolve()), **pipeline(data, call_claude, args.model, args.parallel, verify_model=args.verify_model)}
    analysis, problems = record["analysis"], record["verify_problems"]
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    dest = results / f"{Path(args.reviews).stem}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    dest.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    for section in ("pros", "cons"):
        print(f"\n[{section}]")
        for p in analysis[section]:
            cond = f" ({', '.join(p['conditions'])})" if p["conditions"] else ""
            print(f"  {p['aspect']} x{p['support_count']}: {p['statement']}{cond}")
            for ev in p["evidence"]:
                print(f"      - {ev['quote'][:90]}")
    print("\n[key_opinions]")
    for k in analysis["key_opinions"]:
        print(f"  {k['stance']} x{k['support_count']} {k['aspect']}: {k['text']}")
        for ev in k["evidence"]:
            print(f"      - [{ev['stance']}] {ev['quote'][:90]}")
    rel = analysis["reliability"]
    print(f"\n[reliability] {rel['level']} ({rel['level_word']}) {rel['level_counts']}\n  {rel['reason']}")
    for line in analysis["limitations"]:
        print(f"  - {line}")
    reward = rel["signals"]["reward_mention"]
    print(f"\n[reward_mention] {reward['count']}")
    for ev in reward["evidence"]:
        print(f"  {ev['review_id']}: {ev['quote'][:90]}")
    print(f"\n[exceptions] {len(analysis['exceptions'])}")
    for e in analysis["exceptions"]:
        print(f"  {e['review_id']} {e['type']}: {e['reason']}")
    print(f"\nverify: {'OK' if not problems else f'{len(problems)} problem(s)'}")
    for p in problems:
        print(f"  - {p}")
    print(f"saved: {dest}  ({record['seconds']}s, {record['calls']} calls)")


def reverify(path, model, call=True):
    """Reuse a result's saved map/reduce outputs; useful after changing prompt_verify.md (call=True) or only
    the code steps such as grade.py (call=False, reuses the saved verify output too)."""
    rec = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(Path(rec["input"]).read_text(encoding="utf-8"))
    index = build_index(data)
    started = time.time()
    if call:
        verify_raw = verify(data.get("product", {}), rec["reduce_raw"], rec["map_points"], index, rec["exceptions_raw"], call_claude, model)[0]
    else:
        verify_raw = rec["verify_raw"]
    problems = [p for p in rec["verify_problems"] if p.startswith("B")]  # map-stage problems stay
    rec["analysis"] = finish(data, index, rec["map_points"], rec["reduce_raw"], verify_raw, rec["exceptions_raw"], problems)
    rec.update(verify_raw=verify_raw, verify_problems=problems, reverified_from=path.name)
    if call:
        rec["reverify_seconds"] = round(time.time() - started, 1)
    dest = path.with_name(path.stem.rsplit("-", 2)[0] + f"-{time.strftime('%Y%m%d-%H%M%S')}.json")
    dest.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {dest}" + (f"  (verify {rec['reverify_seconds']}s)" if call else "  (code steps only)"))


def selftest():
    assert split_sentences("2개중에 한개 제품은\n누가 개봉했네요\n✓ C타입\n✓ USB") == ["2개중에 한개 제품은 누가 개봉했네요", "✓ C타입", "✓ USB"]
    assert split_sentences("지원하지 않습니다\n.(갤20)\n고속 충전은 지원.") == ["지원하지 않습니다.(갤20)", "고속 충전은 지원."]
    data = {"product": {"title": "t"}, "reviews": [
        {"review_id": "a", "rating": 5, "body": "소음이 작아요. 좋아요!\n\n세척도 편해요"},
        {"review_id": "b", "rating": None, "body": "배송 기사님이 친절해요"},
        {"review_id": "c", "rating": 4, "body": "조용해요"},
        {"review_id": "d", "rating": 1, "body": "리뷰 좋게 쓰면 상품권 준대요. 시끄러워요"},
        {"review_id": "e", "rating": 5, "body": "최고네요 정말 최고"},
    ]}
    index = build_index(data)
    msg = build_map_message(data["product"], index)
    assert "R1.2 좋아요!" in msg and "R1.3 세척도 편해요" in msg and "R2 ★- | - | -" in msg, msg
    raw = {"points": [
        {"kind": "pro", "aspect": "소음", "statement": "조용", "stance": "positive", "conditions": [],
         "reviews": ["R1", "R3", "R9"], "evidence": ["R1.1", "R3.1"]},
        {"kind": "pro", "aspect": "x", "statement": "x", "stance": "positive", "conditions": [],
         "reviews": ["R2", "R5"], "evidence": ["R2.1", "R1.7", "R5.1"]},
        {"kind": "con", "aspect": "무게", "statement": "무거움", "stance": "negative", "conditions": [],
         "reviews": ["R4"], "evidence": ["R4.2"]}],
        "exceptions": [{"review_id": "R2", "type": "unrelated", "reason": "r"},
                       {"review_id": "R8", "type": "irony", "reason": "r"},
                       {"review_id": "R4", "type": "irony", "reason": "'가성비 갑'이라더니 바꿈"},
                       {"review_id": "R5", "type": "irony", "reason": "'최고네요'는 비꼬는 말"}]}
    points, exceptions, problems = check_map(raw, index, 1)
    # R2: unrelated but cited, so kept. R4: quote not in body, so dropped and kept. R5: irony, counted but never quoted.
    assert [e["review_id"] for e in exceptions] == ["R5"], exceptions
    assert points["B1-P2"]["reviews"] == ["R2", "R5"] and points["B1-P2"]["evidence"] == ["R2.1"], points  # irony counts, not quoted
    assert points["B1-P3"]["reviews"] == ["R4"], points
    assert len(problems) == 6, problems  # R8, R2 kept, R4 dropped, R9, R1.7 unknown, R5.1 exception
    reduce_raw = {"pros": [{"aspect": "소음", "statement": "조용", "conditions": [], "members": ["B1-P1", "B9-P9"],
                            "evidence": ["R3.1", "R1.1", "R4.2"]},
                           {"aspect": "x", "statement": "x", "conditions": [], "members": ["B1-P2", "B1-P1"],
                            "evidence": ["R1.1", "R2.1"]}],
                  "cons": [],
                  "key_opinions": [{"aspect": "무게", "text": "갈림", "stance": "mixed", "members": ["B1-P1", "B1-P3"],
                                    "evidence": ["R3.1"]},
                                   {"aspect": "소움", "text": "같은 항목", "stance": "mixed", "members": ["B1-P1", "B1-P3"],
                                    "evidence": ["R3.1"]},
                                   {"aspect": "x", "text": "x", "stance": "positive", "members": ["B1-P2"],
                                    "evidence": ["R2.1"]}]}
    out = assemble(reduce_raw, points, index, problems)
    pros = {p["aspect"]: p for p in out["pros"]}
    assert pros["소음"]["support_count"] == 2 and [e["quote"] for e in pros["소음"]["evidence"]] == ["조용해요", "소음이 작아요."], pros
    assert [e["review_id"] for e in pros["x"]["evidence"]] == ["b"], pros["x"]  # a was quoted already
    assert [k["aspect"] for k in out["key_opinions"]] == ["무게"], out["key_opinions"]
    assert problems[-5:] == ["pros member B9-P9: unknown point", "C1 pros evidence R4.2: not a member's evidence",
                             "C3 key_opinions 무게: mixed but evidence is one-sided",
                             "C4 key_opinions 소음: same aspect as a pros/cons card, dropped",
                             "C5 key_opinions x: repeats one pros/cons group, dropped"], problems
    # verify path: C1 keeps only the fitting quote, C2 has none and is dropped, mixed C3 gets both sides
    verdicts = {("C1", "R3.1"): {"fits": True, "side": "positive"}, ("C1", "R1.1"): {"fits": False, "side": "positive"},
                ("C2", "R2.1"): {"fits": False, "side": "positive"},
                ("C3", "R3.1"): {"fits": True, "side": "positive"}, ("C3", "R1.1"): {"fits": True, "side": "positive"},
                ("C3", "R4.2"): {"fits": True, "side": "negative"}}
    msg = build_verify_message(data["product"], reduce_raw, points, index, [{"review_id": "R5", "type": "irony"}])
    assert 'C3 [key/mixed] 무게 | 갈림' in msg and '  R4.2 "시끄러워요"' in msg and "R5.1 최고네요 정말 최고" in msg, msg
    problems = []
    out = assemble(reduce_raw, points, index, problems, verdicts=verdicts)
    assert [e["review_id"] for e in out["pros"][0]["evidence"]] == ["c"] and len(out["pros"]) == 1, out["pros"]
    key = out["key_opinions"][0]
    assert [(e["review_id"], e["stance"]) for e in key["evidence"]] == [("a", "positive"), ("d", "negative")], key
    assert "C2 pros x: no quote fits, dropped" in problems, problems
    over = {**reduce_raw, "pros": [{**reduce_raw["pros"][0], "statement": "몇 달 뒤 대부분 조용해진다", "conditions": ["야간"]}] + reduce_raw["pros"][1:]}
    out = assemble(over, points, index, [], card_verdicts={"C1": {"statement_ok": False, "conditions_ok": False}})
    noise = next(r for r in out["pros"] if r["aspect"] == "소음")
    assert noise["statement"] == "조용" and noise["conditions"] == [], noise
    data["reviews"].append({"review_id": "f", "rating": 1, "body": "좋아요 라고 썼었는데 수정합니다. 고장났어요"})
    data["reviews"].append({"review_id": "g", "rating": 1, "body": "박스가 찢겨 왔어요. 버튼도 안 눌려요"})
    data["reviews"].append({"review_id": "h", "rating": 1, "body": "박스파손 제품기스"})
    index = build_index(data)
    kept = check_exceptions([{"review_id": "R6", "type": "irony"}, {"review_id": "R7", "type": "unrelated"},
                             {"review_id": "R8", "type": "unrelated"}, {"review_id": "R2", "type": "unrelated"}], index,
                            {"exceptions": [{"review_id": "R7", "product_sentences": ["R7.2", "R2.1"], "irony": False},
                                            {"review_id": "R2", "product_sentences": [], "irony": False}]}, problems)
    assert kept == [{"review_id": "R2", "type": "unrelated"}], kept
    reward = reward_signal(index)
    assert reward["count"] == 1 and reward["evidence"][0] == {"review_id": "d", "quote": "리뷰 좋게 쓰면 상품권 준대요."}, reward
    copy = "집에서 사용하다가 사은품으로 재구매합니다 고객님들도 좋아해요 포장도 아주 잘되어있습니다"
    idx = build_index({"reviews": [{"review_id": "p", "rating": 4, "body": "흠집 있음"},
                                   {"review_id": "q", "rating": 5, "body": copy},
                                   {"review_id": "r", "rating": 5, "body": copy + " ㅎㅎ"},
                                   {"review_id": "s", "rating": 5, "body": "좋아요"},
                                   {"review_id": "t", "rating": 5, "body": "소리가 작고 세척이 편해요. 단점은 무게."}]})
    sig = grade.rule_signals(idx)
    assert sig["R1"] == {"R2", "R3"} and sig["R2"] == sig["R3"] == {"R1"} and sig["R4"] == {"R3"} and not sig.get("R5"), dict(sig)
    rel = grade.combine(idx, sig, [], 3, 1)
    assert rel["level"] == "LOW" and rel["level_counts"] == {"HIGH": 1, "MEDIUM": 2, "LOW": 2}, rel  # 2 of 5 copied
    assert "3건은 처음부터" in rel["reason"] and "%" not in rel["reason"], rel["reason"]
    part = rules_only({"collection": {"by_rating": []}, "reviews": [{"review_id": "x", "rating": 5, "body": "좋아요"}] * 6}, "test")
    assert part["analysis"]["pros"] == [] and part["analysis"]["reliability"]["level"] in ("HIGH", "MEDIUM", "LOW")
    assert part["analysis"]["limitations"][0].startswith("AI 요약이 실패해서"), part
    sch = {"type": "object", "required": ["points"], "properties": {"points": {"type": "array", "items": {
        "type": "object", "required": ["reviews"], "properties": {"reviews": {"type": "array"}}}}}}
    assert schema_gaps({"points": [{"reviews": []}]}, sch) == []
    assert schema_gaps({}, sch) == ["$.points: missing"] and schema_gaps({"points": [{}]}, sch) == ["$.points[0].reviews: missing"]
    assert coerce({"points": '[{"reviews": []}]'}, sch) == {"points": [{"reviews": []}]}
    two = {"type": "object", "properties": {"pros": {"type": "array"}, "cons": {"type": "array"}}}
    assert coerce({"pros": '[1], "cons": [2]'}, two) == {"pros": [1], "cons": [2]}
    assert coerce({"pros": '[1],\n"cons">[2]'}, two) == {"pros": [1], "cons": [2]}
    assert coerce({"pros": '[1], "cons": [2]\n}'}, two) == {"pros": [1], "cons": [2]}
    cov, lim = grade.coverage({"collection": {"by_rating": [{"rating": 5, "status": "OK", "collected": 1, "total": 1234,
                               "excluded": {"sponsored": 1}, "sponsored": [{"review_id": "z", "snippet": "체험단"}]}]}}, index)
    assert lim[0] == "별점별 범위: 5점 쿠팡 1,234건 중 2건 읽고 1건 분석(뺀 것: 협찬 1)." and cov["by_rating"][0]["sponsored"][0]["review_id"] == "z", (lim, cov)
    assert shipping_only("박스가 찢겨서 왔어요") and not shipping_only("박스는 멀쩡한데 본체에 기스") and not shipping_only("소음이 커요")
    print("selftest OK")

if __name__ == "__main__":
    main()
