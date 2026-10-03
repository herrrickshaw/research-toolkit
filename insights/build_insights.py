#!/usr/bin/env python3
"""Derive insights from the research .md corpus (the same files qmd indexes).

Pure extractive/statistical: TF-IDF + SVD + KMeans, regex claim extraction,
no LLM calls, so the daily run costs no tokens. Incremental where it matters:
claims are extracted once per unique document text, and every run writes a
digest of the docs that are new since the previous run.

Outputs (insights/): research_insights.duckdb, report_latest.md,
digest_<date>.md. Category 'personal' is excluded from everything.

  build_insights.py [--limit N]   # --limit for a smoke test on N docs
"""
import argparse, hashlib, json, re, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import normalize

HOME = Path.home()
TOOLKIT = HOME / "research-toolkit"
OUT = TOOLKIT / "insights"
CATALOG = TOOLKIT / "document_catalog.duckdb"
MEMORY_DIR = HOME / ".claude/projects/-Users-umashankar/memory"
DB = OUT / "research_insights.duckdb"

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
WEB_STOP = list(ENGLISH_STOP_WORDS | {"https", "http", "www", "com", "org", "pdf", "doi", "html", "et", "al", "fig", "cid"})
HEAD_CHARS = 6000        # text used for topic features
HASH_CHARS = 20000       # text used to detect exact-duplicate content
N_CLUSTERS = 100
NEAR_DUP_SIM = 0.97
RECENT_YEAR = 2023
SENSITIVE_MEMORY = re.compile(r"credential|secret|password|token|api[-_ ]?key|passwd", re.I)

# --- claim extraction -------------------------------------------------------
MONEY = re.compile(
    r"(USD|US\$|\$|INR|Rs\.?|₹|EUR|€|GBP|£)\s?(\d[\d,]*(?:\.\d+)?)\s?"
    r"(trillion|billion|million|bn|mn|tn|crore|lakh crore|B|M)\b", re.I)
CAGR = re.compile(r"CAGR[^%\n]{0,50}?(\d{1,2}(?:\.\d+)?)\s?%", re.I)
SUBJECT = re.compile(r"((?:[A-Za-z][A-Za-z\-]+\s+){1,4})market\b", re.I)
YEAR = re.compile(r"\b(20[1-5]\d)\b")
CUE = re.compile(r"size|worth|valued|value of|reach|projected|expected|estimated|revenue|grow", re.I)
STOP = {"the", "a", "an", "of", "in", "for", "and", "to", "by", "this", "that", "its", "their",
        "with", "on", "is", "as", "at", "from", "was", "be", "are", "which", "has", "have", "will",
        "target", "overall", "total", "current", "entire", "whole", "growing", "large", "new"}
CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR",
            "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP"}
UNIT = {"trillion": 1e12, "tn": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
        "m": 1e6, "crore": 1e7, "lakh crore": 1e12}


def subject_of(window: str, money_pos: int):
    """Nearest '<words> market' phrase before the money figure."""
    best = None
    for m in SUBJECT.finditer(window):
        if m.start() <= money_pos:
            best = m
    if not best:
        return None
    toks = [t.lower() for t in best.group(1).split()]
    while toks and toks[0] in STOP:
        toks.pop(0)
    toks = [t for t in toks if t not in {"the", "a"}]
    if not toks or all(t in STOP for t in toks):
        return None
    return " ".join(toks[-3:])


def extract_claims(text: str):
    out = []
    for m in MONEY.finditer(text):
        lo, hi = max(0, m.start() - 160), min(len(text), m.end() + 120)
        w = text[lo:hi]
        if "market" not in w.lower() or not CUE.search(w):
            continue
        subj = subject_of(w, m.start() - lo)
        if not subj:
            continue
        cur = CURRENCY.get(m.group(1).lower(), m.group(1).upper())
        try:
            val = float(m.group(2).replace(",", "")) * UNIT[m.group(3).lower()]
        except (KeyError, ValueError):
            continue
        yrs = [int(y) for y in YEAR.findall(w)]
        out.append(dict(kind="market_size", subject=subj, currency=cur, value=val,
                        year=max(yrs) if yrs else None, snippet=" ".join(w.split())[:280]))
    for m in CAGR.finditer(text):
        lo, hi = max(0, m.start() - 160), min(len(text), m.end() + 60)
        w = text[lo:hi]
        subj = subject_of(w, m.start() - lo)
        if subj:
            out.append(dict(kind="cagr_pct", subject=subj, currency=None, value=float(m.group(1)),
                            year=None, snippet=" ".join(w.split())[:280]))
    return out[:40]  # cap per doc: a market-report boilerplate repeats itself


# --- data loading -----------------------------------------------------------
def read_text(path, n=None):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(n) if n else f.read()
    except OSError:
        return ""


from privacy import (SENSITIVE_DOC, SENSITIVE_TITLE, NAME_DOC, OWN_TEXT, OWN_PATH, PRIVATE_FOLDERS,  # noqa: E402
                     is_sensitive)
GENERIC_SUBJ = {"global", "market", "india", "indian", "world", "worldwide", "domestic", "overall", "total", "us", "usa"}


def clean_title(t):
    return re.sub(r"^[A-Z0-9]{8}_", "", t or "")
YEAR_ANY = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")


def guess_year(text: str):
    """Latest plausible year in the opening pages (publication/copyright year)."""
    ys = [int(y) for y in YEAR_ANY.findall(text[:3000]) if int(y) <= date.today().year]
    return max(ys) if ys else None


def load_catalog(limit):
    c = duckdb.connect(str(CATALOG), read_only=True)
    q = """
      select f.item_key, coalesce(f.title, d.title) title, f.md_path, f.chars, f.converted_at,
             d.category, coalesce(d.auto_topic, d.topic) topic, d.year, d.filetype, d.path
      from fulltext_md f left join documents d using (item_key)
      where f.status='ok' and coalesce(d.category,'') <> 'personal'
      order by f.item_key"""
    rows = c.execute(q).fetchall()
    cols = ["item_key", "title", "md_path", "chars", "converted_at", "category", "topic", "year", "filetype", "path"]
    docs = [dict(zip(cols, r)) for r in rows]
    for d in docs:  # catalog 'year' is free-form text ("unknown", "2019", ...)
        d["title"] = clean_title(d["title"])
        d["folder"] = re.sub(r"^(dropbox:)?/?", "", d["path"] or "").split("/")[0]
        d["kind"] = "own" if d["category"] == "backup" or OWN_PATH.search(d["path"] or "") else "literature"
        y = str(d["year"] or "").strip()
        d["year"] = int(y) if y.isdigit() and 1950 <= int(y) <= date.today().year else None
    return docs[:limit] if limit else docs


def memory_notes():
    notes = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md" or SENSITIVE_MEMORY.search(p.stem):
            continue
        txt = read_text(p, 3000)
        m = re.search(r"^description:\s*(.+)$", txt, re.M)
        desc = (m.group(1).strip().strip('"') if m else "")[:200]
        if SENSITIVE_MEMORY.search(desc):
            continue
        body = re.sub(r"^---.*?---", "", txt, count=1, flags=re.S)
        notes.append(dict(name=p.stem, description=desc, text=desc + " " + body))
    return notes


# --- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    db = duckdb.connect(str(OUT / "smoke.duckdb") if args.limit else str(DB))
    db.execute("""create table if not exists docs(item_key varchar primary key, title varchar,
        category varchar, topic varchar, year integer, chars bigint, text_hash varchar,
        cluster integer, is_canonical boolean, first_seen date, converted_at timestamp, kind varchar, shareable boolean)""")
    db.execute("""create table if not exists claims(item_key varchar, text_hash varchar, kind varchar,
        subject varchar, currency varchar, value double, year integer, snippet varchar)""")
    db.execute("create table if not exists claims_done(text_hash varchar primary key)")
    db.execute("create table if not exists run_log(run_at timestamp, docs integer, unique_docs integer, note varchar)")
    prev = {r[0]: r[1] for r in db.execute("select item_key, first_seen from docs").fetchall()}
    first_run = not prev
    today = date.today()

    docs = load_catalog(args.limit)
    print(f"[1/6] catalog: {len(docs)} converted docs (personal excluded)", flush=True)

    # exact-duplicate detection + heads
    by_hash, heads = defaultdict(list), {}
    for d in docs:
        txt = read_text(d["md_path"], HASH_CHARS)
        norm = " ".join(txt.lower().split())
        d["text_hash"] = hashlib.sha1(norm.encode()).hexdigest()
        by_hash[d["text_hash"]].append(d)
        heads[d["item_key"]] = " ".join(txt[:HEAD_CHARS].split())
        if not d["year"]:
            d["year"] = guess_year(txt)
        d["first_seen"] = prev.get(d["item_key"], today)
    for d in docs:  # own-project markers in the text reclassify saved pages/reports as own output
        if d["kind"] == "literature" and OWN_TEXT.search(heads[d["item_key"]][:2500]):
            d["kind"] = "own"
    sens = {d["item_key"] for d in docs if SENSITIVE_DOC.search(d["title"] + " " + heads[d["item_key"]][:3000])
            or SENSITIVE_TITLE.search(d["title"])
            or (d["kind"] == "literature" and NAME_DOC.search(d["title"] + " " + heads[d["item_key"]][:3000]))}
    docs = [d for d in docs if d["item_key"] not in sens]
    by_hash = {h: [d for d in g if d["item_key"] not in sens] for h, g in by_hash.items()}
    by_hash = {h: g for h, g in by_hash.items() if g}
    print(f"      sensitive-content filter dropped {len(sens)} docs from all outputs", flush=True)
    canon = []
    for h, grp in by_hash.items():
        grp.sort(key=lambda d: (d["kind"] == "own", d["category"] == "backup", -(d["chars"] or 0), d["item_key"]))
        for i, d in enumerate(grp):
            d["is_canonical"] = i == 0
            d["kind"] = grp[0]["kind"]  # a text that exists as literature anywhere is literature
        canon.append(grp[0])
    # blank/near-empty extractions all hash alike; keep them out of clustering
    canon = [d for d in canon if len(heads[d["item_key"]]) >= 400]
    canon_own = [d for d in canon if d["kind"] == "own"]
    canon = [d for d in canon if d["kind"] == "literature"]
    print(f"[2/6] {len(by_hash)} unique texts ({len(docs) - len(by_hash)} exact copies); "
          f"{len(canon)} literature + {len(canon_own)} own substantive  ({time.time()-t0:.0f}s)", flush=True)

    # topic clusters
    vec = TfidfVectorizer(max_features=60000, min_df=5, max_df=0.35, stop_words=WEB_STOP,
                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]{2,}\b", sublinear_tf=True, ngram_range=(1, 2), dtype=np.float32)
    X = vec.fit_transform([heads[d["item_key"]] for d in canon])
    k = max(8, min(N_CLUSTERS, len(canon) // 40))
    svd = TruncatedSVD(n_components=min(150, X.shape[1] - 1), random_state=0)
    Z = normalize(svd.fit_transform(X))
    km = MiniBatchKMeans(n_clusters=k, random_state=0, n_init=3, batch_size=2048).fit(Z)
    labels = km.labels_
    terms = np.array(vec.get_feature_names_out())
    gmean = np.asarray(X.mean(axis=0)).ravel()
    Xo = vec.transform([heads[d["item_key"]] for d in canon_own]) if canon_own else None
    own_labels = km.predict(normalize(svd.transform(Xo))) if canon_own else []
    own_count = np.bincount(own_labels, minlength=k) if canon_own else np.zeros(k, int)
    cluster_rows = []
    for c in range(k):
        idx = np.where(labels == c)[0]
        if not len(idx):
            continue
        cm = np.asarray(X[idx].mean(axis=0)).ravel()
        top = terms[np.argsort(cm - gmean)[::-1][:6]]
        yrs = [canon[i]["year"] for i in idx if canon[i]["year"]]
        cluster_rows.append(dict(cluster=c, label=", ".join(top), n_docs=len(idx),
                                 median_year=int(np.median(yrs)) if yrs else None,
                                 pct_recent=round(100 * sum(y >= RECENT_YEAR for y in yrs) / len(yrs), 1) if yrs else None,
                                 with_year=len(yrs), n_own=int(own_count[c])))
    cl_of = {canon[i]["item_key"]: int(labels[i]) for i in range(len(canon))}
    hash_cluster = {canon[i]["text_hash"]: int(labels[i]) for i in range(len(canon))}
    hash_cluster.update({canon_own[i]["text_hash"]: int(own_labels[i]) for i in range(len(canon_own))})
    for d in docs:
        d["cluster"] = hash_cluster.get(d["text_hash"])
    print(f"[3/6] {k} clusters ({time.time()-t0:.0f}s)", flush=True)

    def shareable(d):  # may this doc's title appear in a report / the uploaded DB?
        return d["category"] == "research" and d["folder"] not in PRIVATE_FOLDERS

    for d in docs:
        d["shareable"] = shareable(d)

    # near-duplicates (different text, same content family: editions, preprints)
    nn = NearestNeighbors(n_neighbors=min(6, len(canon)), metric="cosine", algorithm="brute").fit(Z)
    dist, ind = nn.kneighbors(Z)
    near = []
    for i in range(len(canon)):
        for dj, j in zip(dist[i][1:], ind[i][1:]):
            if j > i and 1 - dj >= NEAR_DUP_SIM and shareable(canon[i]) and shareable(canon[j]):
                near.append((canon[i]["item_key"], canon[i]["title"], canon[j]["item_key"], canon[j]["title"], round(1 - float(dj), 4)))
    print(f"[4/6] {len(near)} near-duplicate pairs ({time.time()-t0:.0f}s)", flush=True)

    # claims: only for unique texts we have not scanned before
    done = {r[0] for r in db.execute("select text_hash from claims_done").fetchall()}
    new_claims, new_done = [], []
    for h, grp in by_hash.items():
        if h in done:
            continue
        d = grp[0]
        if d["kind"] != "literature" or not d["shareable"]:
            continue
        for cl in extract_claims(read_text(d["md_path"])):
            new_claims.append((d["item_key"], h, cl["kind"], cl["subject"], cl["currency"],
                               cl["value"], cl["year"], cl["snippet"]))
        new_done.append((h,))
    print(f"[5/6] claims: scanned {len(new_done)} new texts -> {len(new_claims)} claims ({time.time()-t0:.0f}s)", flush=True)

    # cross-source links: memory decisions <-> research clusters/docs
    notes = memory_notes()
    links = []
    if notes:
        M = vec.transform([n["text"] for n in notes])
        sims = (M @ X.T).toarray() if X.shape[0] * len(notes) < 5e7 else None
        for ni, n in enumerate(notes):
            row = sims[ni] if sims is not None else (M[ni] @ X.T).toarray().ravel()
            for j in [j for j in np.argsort(row)[::-1][:40] if shareable(canon[j])][:5]:
                links.append((n["name"], n["description"], canon[j]["item_key"], canon[j]["title"],
                              int(labels[j]), round(float(row[j]), 4)))

    # persist
    db.execute("begin")
    db.execute("delete from docs")
    db.executemany("insert into docs values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   [(d["item_key"], d["title"] if d["shareable"] else None, d["category"], d["topic"], d["year"], d["chars"], d["text_hash"],
                     d["cluster"], d["is_canonical"], d["first_seen"], d["converted_at"], d["kind"], d["shareable"]) for d in docs])
    for t, rows in (("clusters", cluster_rows),):
        db.execute(f"drop table if exists {t}")
        db.execute("create table clusters(cluster int, label varchar, n_docs int, median_year int, pct_recent double, with_year int, n_own int)")
        db.executemany("insert into clusters values (?,?,?,?,?,?,?)",
                       [(r["cluster"], r["label"], r["n_docs"], r["median_year"], r["pct_recent"], r["with_year"], r["n_own"]) for r in rows])
    db.execute("drop table if exists near_dups")
    db.execute("create table near_dups(a_key varchar, a_title varchar, b_key varchar, b_title varchar, similarity double)")
    db.executemany("insert into near_dups values (?,?,?,?,?)", near)
    db.execute("drop table if exists memory_links")
    db.execute("create table memory_links(memory varchar, description varchar, doc_key varchar, doc_title varchar, cluster int, similarity double)")
    db.executemany("insert into memory_links values (?,?,?,?,?,?)", links)
    if new_claims:
        db.executemany("insert into claims values (?,?,?,?,?,?,?,?)", new_claims)
    if new_done:
        db.executemany("insert or ignore into claims_done values (?)", new_done)
    db.execute("commit")

    # contradictions: same subject+currency+year, unique texts disagree >=2x
    db.execute("drop table if exists contradictions")
    db.execute("""create table contradictions as
      with m as (select subject, currency, year, text_hash, any_value(item_key) item_key,
                        median(value) v, any_value(snippet) snippet
                 from claims where kind='market_size' and year is not null and value >= 1e7
                 group by subject, currency, year, text_hash)
      select subject, currency, year, count(*) n_docs, min(v) lo, max(v) hi, max(v)/min(v) ratio,
             arg_min(item_key, v) lo_key, arg_max(item_key, v) hi_key,
             arg_min(snippet, v) lo_snippet, arg_max(snippet, v) hi_snippet
      from m where subject not in (select unnest(?::varchar[]))
      group by 1,2,3 having count(*) >= 2 and max(v)/min(v) >= 2 and max(v)/min(v) <= 15
      order by n_docs desc, ratio desc""", [sorted(GENERIC_SUBJ)])
    db.execute("insert into run_log values (?,?,?,?)",
               [datetime.now(), len(docs), len(by_hash), "smoke" if args.limit else "full"])
    print(f"[6/6] persisted ({time.time()-t0:.0f}s)", flush=True)

    write_reports(db, docs, by_hash, first_run, today)
    db.close()


# --- reports ----------------------------------------------------------------
def fmt_money(v, cur):
    for div, s in ((1e12, "tn"), (1e9, "bn"), (1e6, "mn")):
        if v >= div:
            return f"{cur} {v/div:,.1f}{s}"
    return f"{cur} {v:,.0f}"


def write_reports(db, docs, by_hash, first_run, today):
    q = lambda s, *a: db.execute(s, list(a)).fetchall()
    n_docs, n_uniq = len(docs), len(by_hash)
    dup_copies = n_docs - n_uniq
    kinds = dict(q("select kind, count(*) from docs where is_canonical group by 1"))
    kind_all = dict(q("select kind, count(*) from docs group by 1"))
    lines = [f"# Research corpus insights — {today}", "",
             f"- **{n_docs:,}** converted documents → **{n_uniq:,}** unique texts; "
             f"**{dup_copies:,}** ({100*dup_copies/max(n_docs,1):.0f}%) are exact-content copies of another document.",
             f"- Unique texts: **{kinds.get('literature', 0):,} literature** (papers, books, reports, saved pages) and "
             f"**{kinds.get('own', 0):,} your own output / machine backups** "
             f"(before dedupe: {kind_all.get('literature', 0):,} vs {kind_all.get('own', 0):,}). Only literature is used for the tables below, "
             "except the write-vs-read section.",
             f"- Claims indexed: {q('select count(*) from claims')[0][0]:,} "
             f"({q('select count(*) from claims where kind=$$market_size$$')[0][0]:,} market-size figures)", ""]

    big = q("select cluster,label,n_docs,median_year,pct_recent from clusters order by n_docs desc limit 15")
    lines += ["## Where the corpus is thick (largest topic clusters)", "",
              "| # | Cluster (top terms) | Docs | Median year | % since 2023 |", "|---|---|---:|---:|---:|"]
    lines += [f"| {c} | {l} | {n:,} | {y or '–'} | {p if p is not None else '–'} |" for c, l, n, y, p in big]

    thin = q("""select cluster,label,n_docs,median_year,pct_recent from clusters
                where with_year >= 10 and pct_recent >= 35 and n_docs <= (select median(n_docs) from clusters)
                order by pct_recent desc, n_docs limit 12""")
    stale = q("""select cluster,label,n_docs,median_year,pct_recent from clusters
                 where with_year >= 10 and median_year <= 2018 and n_docs >= (select median(n_docs) from clusters)
                 order by median_year, n_docs desc limit 12""")
    lines += ["", "## Gaps", "", "**Growing but thin** — mostly recent papers, few of them (candidates to add to):", ""]
    lines += [f"- {l} — {n} docs, {p}% since {RECENT_YEAR}" for c, l, n, y, p in thin] or ["- none flagged"]
    lines += ["", "**Substantial but stale** — median year ≤ 2018 (candidates for a refresh):", ""]
    lines += [f"- {l} — {n} docs, median year {y}" for c, l, n, y, p in stale] or ["- none flagged"]

    wr = q("""select label, n_docs, n_own, median_year from clusters where n_own >= 15 and n_own > 2*n_docs order by n_own desc limit 10""")
    rd = q("""select label, n_docs, n_own from clusters where n_docs >= 60 and n_own <= n_docs/20 order by n_docs desc limit 10""")
    lines += ["", "## What you write vs. what you read", "",
              "**You write about it, but the library is thin** (own output ≫ literature — claims resting on few sources):", ""]
    lines += [f"- {l} — {no} own docs vs {n} literature" for l, n, no, y in wr] or ["- none flagged"]
    lines += ["", "**You collect it but haven't written on it** (literature-heavy, almost no own output — unmined reading):", ""]
    lines += [f"- {l} — {n} literature docs, {no} own" for l, n, no in rd] or ["- none flagged"]

    unlinked = q("""select c.cluster, c.label, c.n_docs from clusters c
                    left join (select cluster, count(*) k from memory_links where similarity >= 0.08 group by 1) l
                    using (cluster) where l.k is null order by c.n_docs desc limit 12""")
    strong = q("""select memory, doc_title, similarity from memory_links
                  qualify row_number() over (partition by memory order by similarity desc) = 1
                  order by similarity desc limit 12""")
    lines += ["", "## Cross-source links (memory ↔ research)", "",
              "**Memory notes with the closest research evidence** (best-matching corpus document per note):", ""]
    lines += [f"- `{m}` ← {t[:90]} (sim {s:.2f})" for m, t, s in strong]
    lines += ["", "**Large research clusters that no memory note touches** (unused evidence):", ""]
    lines += [f"- {l} — {n:,} docs" for c, l, n in unlinked] or ["- every cluster is linked"]

    contra = q("select subject,currency,year,n_docs,lo,hi,ratio,lo_snippet,hi_snippet from contradictions limit 25")
    lines += ["", "## Conflicting market-size claims", "",
              "Same market phrase, currency and target year, different documents, ≥2× apart. "
              "Regex extraction — read the snippets before trusting a flag.", ""]
    for s, cur, y, n, lo, hi, r, ls, hs in contra:
        lines += [f"- **{s} market, {y}** — {n} docs, {fmt_money(lo, cur)} → {fmt_money(hi, cur)} ({r:.1f}×)",
                  f"  - low: “{ls[:200]}”", f"  - high: “{hs[:200]}”"]
    if not contra:
        lines.append("- none yet (needs ≥2 documents quoting the same market and year)")

    same = q("select count(*) from near_dups where similarity >= 0.995")[0][0]
    nd = q("select a_title,b_title,similarity from near_dups where similarity < 0.995 order by similarity desc limit 10")
    lines += ["", "## Near-duplicate pairs (97–99.5% similar: editions, preprints, revised versions)", "",
              f"{same:,} further pairs are ≥99.5% similar (same file saved under different names) and are omitted.", ""]
    lines += [f"- {a[:70]} ≈ {b[:70]} ({s:.3f})" for a, b, s in nd] or ["- none"]
    (OUT / "report_latest.md").write_text("\n".join(lines) + "\n")

    # digest of new documents
    if first_run:
        cutoff = datetime.combine(today - timedelta(days=7), datetime.min.time())
        new = q("select item_key,title,category,year,cluster,chars from docs where converted_at >= ? and is_canonical and kind='literature' and shareable", cutoff)
        head = "baseline digest: documents converted in the last 7 days"
    else:
        new = q("select item_key,title,category,year,cluster,chars from docs where first_seen = ? and is_canonical and kind='literature' and shareable", today)
        head = "documents new since the previous run"
    lab = {c: l for c, l, *_ in q("select cluster,label from clusters")}
    dl = [f"# Research digest — {today}", "", f"{len(new)} {head}.", ""]
    for key, title, cat, yr, cl, chars in new[:200]:
        cls = q("select subject,value,currency,year from claims where item_key=? and kind='market_size' limit 2", key)
        extra = "; ".join(f"{s} market {fmt_money(v, c)}{f' by {y}' if y else ''}" for s, v, c, y in cls)
        dl.append(f"- **{(title or key)[:90]}** ({cat}, {yr or 'n/a'}, {chars or 0:,} chars) — "
                  f"cluster: {lab.get(cl, '–')[:60]}" + (f" — {extra}" if extra else ""))
    (OUT / f"digest_{today}.md").write_text("\n".join(dl) + "\n")
    print(f"reports: {OUT/'report_latest.md'} | {OUT / f'digest_{today}.md'}")


if __name__ == "__main__":
    sys.exit(main())
