#!/usr/bin/env python3
"""
Subject-matter classifier for document_catalog.duckdb, complementary to
categorize.py's `topic` column: that one is folder-derived (WHERE the file
was stored), this one is title-derived (WHAT the file is about). The two
disagree often and that's expected - a paper on cybersecurity sitting in a
generic "Pdf" backup folder gets topic="Pdf" but auto_topic="IT / Software /
Computer Science".

Origin: promoted from an ad hoc classifier written during the 2026-09-16
"deeper sweep" topic-bucketing pass (see topic_sweep_2026-09-16.md). That
pass found the honest result - ~89-92% of this catalog is generic
machine/cloud-backup mass with no recoverable subject, confirmed two
independent ways (folder-topic and this keyword classifier landed within 2.5
points of each other). Re-running this script won't change that: it's a
ceiling of what a keyword-on-title approach can find, not a bug to fix by
adding more keywords. Extend CATEGORIES when a real, recurring subject is
missing (e.g. "domain-driven design" was added after Eric Evans' DDD book
turned up unclassified in that sweep) - not to chase the unclassified rate
down for its own sake.

Idempotent: always recomputes auto_topic for every row and overwrites the
column, so it's safe to rerun after adding categories or after new imports.
Pure local transform over an already-built catalog - no network calls, no
checkpointing needed (13k rows classifies in well under a second).
"""
import re
import sys

import duckdb

DB_PATH = "/Users/umashankar/research-toolkit/document_catalog.duckdb"


def wb(*words):
    """Word-boundary regex for each phrase; spaces/hyphens/underscores are
    interchangeable so "object oriented", "object-oriented" and
    "object_oriented" all match one pattern."""
    return [r"\b" + re.escape(w).replace(r"\ ", r"[\s_\-]+") + r"\b" for w in words]


# Ordered most-specific-first: a title is classified into the first category
# whose pattern matches. Document-type buckets (Personal/admin, Business,
# Reference, Reports) sit after every subject-matter category so a domain
# report (e.g. "India Trade Report 2026") lands in its subject, not in the
# generic bucket.
CATEGORIES = [
    ("IT / Software / Computer Science", wb(
        "software engineering", "computer science", "algorithm", "algorithms", "database", "databases",
        "programming", "kubernetes", "docker", "devops", "microservice", "microservices", "cloud computing",
        "machine learning", "artificial intelligence", "deep learning", "neural network", "neural networks",
        "cybersecurity", "cyber security", "sql", "nosql", "blockchain", "distributed system",
        "distributed systems", "operating system", "compiler", "data structure", "data structures",
        "software architecture", "design pattern", "design patterns", "agile", "scrum", "togaf", "object oriented",
        "object-oriented", "python", "javascript", "typescript", "golang",
        "web development", "frontend", "backend", "full stack", "fullstack", "github", "saas",
        "data science", "data engineering", "big data", "hadoop", "data warehouse",
        "information technology", "network security", "penetration testing", "encryption",
        "cryptography", "natural language processing", "computer vision", "reinforcement learning",
        "software testing", "unit testing", "domain-driven design", "domain driven design",
    )),
    ("Finance / Investing / Markets", wb(
        "stock market", "equity", "equities", "portfolio", "investment", "investing", "valuation", "financial model",
        "financial analysis", "discounted cash flow", "hedge fund", "mutual fund", "derivatives",
        "options trading", "forex", "banking", "private equity", "venture capital", "merger", "acquisition",
        "bond market", "fixed income", "trading strategy", "technical analysis", "fundamental analysis",
        "risk management", "asset management", "wealth management", "capital market", "balance sheet",
        "income statement", "cash flow statement", "nasdaq", "dividend", "interest rate",
        "monetary policy", "cryptocurrency", "bitcoin", "stock exchange", "nse", "bse", "ipo",
    )),
    ("Energy (power, fuel, batteries)", wb(
        "battery", "batteries", "hydrogen", "ethanol", "biofuel", "solar energy", "solar power", "wind energy",
        "renewable energy", "electrolyser", "electrolyzer", "power grid", "smart grid", "energy storage",
        "fuel cell", "oil and gas", "petroleum", "natural gas", "nuclear power", "photovoltaic",
        "lithium ion", "lithium-ion", "energy transition", "power plant", "electricity generation",
    )),
    ("Automotive & EVs", wb(
        "automotive", "electric vehicle", "electric vehicles", "vehicle engineering", "engine design",
        "transmission system", "automobile", "car design", "autonomous vehicle",
    )),
    ("Aviation & Aerospace", wb(
        "aviation", "aircraft", "aerospace", "airline", "sustainable aviation fuel", "aeronautical", "airport",
    )),
    ("Medical / Healthcare", wb(
        "medical", "healthcare", "health care", "clinical", "patient", "disease", "pharma", "pharmaceutical",
        "hospital", "nursing", "cardiac", "cardiology", "cancer", "oncology", "diabetes", "surgery", "surgical",
        "diagnosis", "therapy", "vaccine", "prenatal", "obstetric", "radiology", "pathology", "psychiatry",
        "public health", "epidemiology",
    )),
    ("India Trade / Economic Policy", wb(
        "trade policy", "export", "import", "tariff", "economic survey", "fiscal deficit", "gst",
        "wpi", "cpi", "rbi", "current account", "balance of payment", "mospi", "niti aayog", "budget speech",
        "trade deficit", "foreign exchange reserve", "economic growth",
    )),
    ("Agriculture", wb(
        "agriculture", "agricultural", "agri commodity", "farming", "farmer", "mandi", "irrigation",
        "fertilizer", "apmc", "horticulture", "livestock", "dairy farming",
    )),
    ("Education / Exams", wb(
        "cbse", "syllabus", "question paper", "curriculum", "entrance exam", "board exam", "ncert",
    )),
    ("Chemistry / Materials Science", wb(
        "chemical engineering", "polymer", "catalysis", "material science", "materials science", "organic chemistry",
        "inorganic chemistry", "nanomaterial", "petrochemical",
    )),
    ("Government / Policy / Regulation", wb(
        "ministry of", "gazette", "parivesh", "government scheme", "regulatory framework",
        "parliament", "lok sabha", "rajya sabha", "white paper",
    )),
    ("Law / Legal", wb(
        "legal framework", "supreme court", "high court", "statute", "contract law", "intellectual property",
        "arbitration", "litigation", "legal opinion", "case law",
    )),
    ("Engineering (general)", wb(
        "mechanical engineering", "electrical engineering", "civil engineering", "structural engineering",
        "thermodynamics", "control system", "control systems", "fluid mechanics", "heat transfer",
        "manufacturing process", "robotics",
    )),
    ("Sports / Cricket", wb("cricket", "test match", "batting average", "bowling figures")),
    ("Environment / Climate", wb(
        "climate change", "carbon emission", "carbon footprint", "environmental impact", "sustainability report",
        "greenhouse gas", "pollution control", "biodiversity", "emission reduction", "net zero",
    )),
    ("Personal & administrative documents", wb(
        "resume", "curriculum vitae", "cover letter", "bank statement", "bank stmt", "invoice", "receipt",
        "screenshot", "office lens", "interview prep", "job application",
    )),
    ("Business, tender & corporate documents", wb(
        "tender", "brochure", "press release", "request for proposal", "memorandum of understanding",
        "purchase order", "quotation", "corporate profile", "annual report",
    )),
    ("Reference material (Wikipedia / how-to)", wb("wikipedia", "buying guide", "user guide", "how to")),
    ("Reports & presentations (general)", wb("report", "presentation", "overview", "whitepaper", "white paper")),
]

UNCLASSIFIED = "Other / Unclassified"


def classify(title):
    t = (title or "").lower()
    for name, patterns in CATEGORIES:
        for pattern in patterns:
            if re.search(pattern, t):
                return name
    return UNCLASSIFIED


def main():
    report_only = "--report" in sys.argv

    con = duckdb.connect(DB_PATH, read_only=report_only)
    rows = con.execute("SELECT item_key, title FROM documents").fetchall()

    classified = [(classify(title), item_key) for item_key, title in rows]

    counts = {}
    for topic, _ in classified:
        counts[topic] = counts.get(topic, 0) + 1
    total = len(classified)
    print(f"{total} documents classified\n")
    for topic, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{n:6d} ({100 * n / total:5.1f}%)  {topic}")

    if report_only:
        print("\n(--report: DB not modified)")
        con.close()
        return

    con.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS auto_topic VARCHAR")
    con.execute("CREATE OR REPLACE TEMP TABLE _auto_topic (item_key VARCHAR, auto_topic VARCHAR)")
    con.executemany("INSERT INTO _auto_topic VALUES (?, ?)", [(k, t) for t, k in classified])
    con.execute("""
        UPDATE documents
        SET auto_topic = _auto_topic.auto_topic
        FROM _auto_topic
        WHERE documents.item_key = _auto_topic.item_key
    """)
    con.execute("DROP TABLE _auto_topic")

    print(f"\n=== auto_topic column written for {total} rows in {DB_PATH} ===")
    con.close()


if __name__ == "__main__":
    main()
