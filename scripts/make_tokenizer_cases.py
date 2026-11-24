"""Make tests/data/mclip_tokens.json: reference token ids of the official `tokenizers` library.

The app (ratedgallery/search.py) and the phone app (WordPiece.kt) have their own copy of the WordPiece
tokenizer; their tests compare it with these cases. Developer tool only, not used by the app.

  python -m venv tokenv && tokenv\\Scripts\\pip install tokenizers==0.22.1
  download tokenizer.json of sentence-transformers/clip-ViT-B-32-multilingual-v1
  (SHA-256 5b4e1a8171c81dfd666ae40265b9530c6e0b3d53923fe8ac493dcc84229adf81, see ratedgallery/setup_models.py)
  tokenv\\Scripts\\python scripts\\make_tokenizer_cases.py path\\to\\tokenizer.json
"""
import json
import sys
from pathlib import Path

from tokenizers import Tokenizer

QUERIES = [
    "river", "sunset at the beach", "Sunset At The BEACH", "a dog playing in the snow", "me with sunglasses",
    "birthday cake", "New York City at night", "3 people on a boat", "no people", "selfie, smiling!",
    "mountains & lakes (summer 2019)", "e-mail: test@example.com", "don't stop", "$100 dinner #food",
    "unbelievably photogenic", "Schrödinger's café", "naïve résumé", "Zürich Hauptbahnhof", "São Paulo",
    "עוגת יום הולדת", "חוף הים בשקיעה", "אני עם משקפי שמש", "ירושלים, 2020", "שקיעה at the beach",
    "день рождения", "праздничный торт", "عيد ميلاد سعيد", "お誕生日ケーキ", "生日蛋糕", "x中文y",
    "Ελληνικά νησιά", "पहाड़ों में सूर्योदय", "🎂 birthday", "party 🎉🎉", "tab\there", "line\nbreak",
    "zero​width", "soft­hyphen", "  spaces   around  ", "", "!!!", "...and then?",
    "a" * 101, "supercalifragilisticexpialidocious", " ".join(["word"] * 200),
    " ".join(["שלום"] * 150), "COVID-19 vaccination 2021", "iPhone 15 Pro", "4K HDR photo", "l'été à Paris",
]


def main():
    tok = Tokenizer.from_file(sys.argv[1])
    tok.enable_truncation(128)
    tok.no_padding()
    cases = [{"q": q, "ids": tok.encode(q).ids} for q in QUERIES]
    out = Path(__file__).resolve().parent.parent / "tests" / "data" / "mclip_tokens.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{len(cases)} cases -> {out}")


if __name__ == "__main__":
    main()
