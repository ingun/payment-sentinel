"""
Runbook dokümanları üzerinde kaynaklı arama (BM25).

Dokümanlar Markdown başlıklarına göre bölümlere ayrılır; her parça kaynak dosya
ve bölüm adını taşır. Doküman içeriği güvenilmeyen veri kabul edilir.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from src.config import RUNBOOK_DIR

MIN_SCORE = 1.0
MAX_CHUNK_CHARS = 700
PROCEDURE_SECTION = "İnceleme Adımları"

# Doküman içine gömülü talimatları ayıklamak için basit desenler
INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"önceki talimatları (yok say|unut)",
        r"system prompt",
        r"you are now",
        r"root cause (is )?confirmed",
    ]
]


@dataclass
class Chunk:
    source: str
    section: str
    text: str
    flagged: bool = False


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[\wçğıöşü]+", text.lower())
    expanded = []
    for token in tokens:
        expanded.append(token)
        if "_" in token or "-" in token:
            expanded.extend(t for t in re.split(r"[_-]", token) if t)
    return expanded


def sanitize(text: str) -> tuple[str, bool]:
    """Talimat içeren satırları çıkarır; içerik veri olarak kalır."""
    kept, flagged = [], False
    for line in text.splitlines():
        if any(p.search(line) for p in INJECTION_PATTERNS):
            flagged = True
            kept.append("[güvenlik nedeniyle çıkarıldı: doküman içi talimat]")
        else:
            kept.append(line)
    return "\n".join(kept), flagged


def split_markdown(source: str, text: str) -> list[Chunk]:
    title = source
    chunks: list[Chunk] = []
    section, lines = "Genel", []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if not body:
            return
        clean, flagged = sanitize(body)
        for i in range(0, len(clean), MAX_CHUNK_CHARS):
            chunks.append(Chunk(source, f"{title} › {section}", clean[i : i + MAX_CHUNK_CHARS], flagged))

    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            section, lines = line[3:].strip(), []
        else:
            lines.append(line)
    flush()
    return chunks


class RunbookRetriever:
    def __init__(self, runbook_dir: Path = RUNBOOK_DIR, min_score: float = MIN_SCORE):
        self.min_score = min_score
        self.chunks: list[Chunk] = []
        for path in sorted(runbook_dir.glob("*.md")):
            self.chunks.extend(split_markdown(path.name, path.read_text(encoding="utf-8")))
        if not self.chunks:
            raise FileNotFoundError(f"Runbook bulunamadı: {runbook_dir}")
        # Başlık da aranabilir olsun diye bölüm adı metne eklenir
        self.bm25 = BM25Okapi([tokenize(f"{c.section} {c.text}") for c in self.chunks])

    def search(self, query: str, top_k: int = 3) -> dict:
        tokens = tokenize(query or "")
        if not tokens:
            return {"query": query, "found": False, "hits": [], "message": "Boş sorgu."}
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(zip(scores, self.chunks), key=lambda x: x[0], reverse=True)
        hits = [
            {"source": c.source, "section": c.section, "text": c.text, "score": round(float(s), 2), "flagged": c.flagged}
            for s, c in ranked[:top_k]
            if s >= self.min_score
        ]
        message = (
            f"{len(hits)} ilgili runbook parçası bulundu."
            if hits
            else "İlgili runbook bulunamadı. Öneriler yalnızca gözlemlere dayanacak."
        )
        return {"query": query, "found": bool(hits), "hits": hits, "procedures": self.procedures(hits), "message": message}

    def procedures(self, hits: list[dict], max_sources: int = 2) -> list[dict]:
        """Eşleşen dokümanların 'İnceleme Adımları' bölümünü kaynağıyla döner."""
        sources = list(dict.fromkeys(h["source"] for h in hits))[:max_sources]
        result = []
        for source in sources:
            for chunk in self.chunks:
                if chunk.source == source and chunk.section.endswith(PROCEDURE_SECTION):
                    result.append({"source": source, "section": chunk.section, "steps": extract_steps(chunk.text)})
                    break
        return result


def extract_steps(text: str, limit: int = 5) -> list[str]:
    """Parçadaki madde işaretli adımları çıkarır."""
    steps = [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]
    return [s for s in steps if not s.startswith("[güvenlik")][:limit]
