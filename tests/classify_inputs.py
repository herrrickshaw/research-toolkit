"""Fixed synthetic inputs shared by the golden generator and the tests."""
PATHS = [
    "Personal Docs/ITR 2020.pdf", "Backups/laptop_backup/x.pdf", "pdf/Employment(Partial) 3/cv.pdf",
    "zip/a.pdf", "loose.pdf", "Documents/Energy/x.epub", "Research/Hydrogen/paper.pdf",
    "Downloads/sub/a.zip!inner/file.PDF", "My Drive/foo/bar.docx", "docs/x/y/z.pptx", "Saved from Web/a.html",
    "Photos/camera upload/a.jpg", "Finance/Valuation/book.mobi", "noext/file", "Type/abc/d.ris",
]
MTIMES = [0, None, 1_700_000_000_000, 946_684_800_000, -1, 10 ** 20, 253_402_300_800_000]
FOLDERS = ["", None, "Personal", "Kopia backup", "Documents", "Research", "Resume 2019", "Downloads", "MY PC", "Sent Files"]
TITLES = [
    "Deep Learning with Python", "Stock Market Valuation Handbook", "Hydrogen Electrolyser Design",
    "Electric Vehicle Battery Pack", "Annual Report 2021", "Bank Statement March", "CBSE Question Paper Class 10",
    "Random Untitled Scan 001", "", None, "Domain-Driven Design", "Object_Oriented Programming", "Cricket batting average",
    "India Trade Report 2026", "How to bake bread", "ITR acknowledgement", "Clinical trial of vaccine",
]
