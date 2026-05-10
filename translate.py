# переводим srt файлы, оставляем те же временные метки

import sys
import argostranslate.translate
import re
from pathlib import Path

def translate_text(text: str, from_lang, to_lang) -> str:
    if not text or not text.strip():
        return ""
    try:
        return argostranslate.translate.translate(text.strip(), from_lang, to_lang)
    except:
        return "[Ошибка перевода]"


def read_srt_file(file_path: Path) -> list[list[str]]:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n(?=\d+\n)', content.strip() + '\n')
    subtitles = {}
    for b in blocks:
        lines = [line.strip() for line in b.split('\n') if line.strip()]
        if len(lines) >= 2:
            subtitles[lines[1]] = " ".join(lines[2:])
        else:
            print(f"Warning: Block {b} has too few lines. Skipping.")
    return subtitles


def main(input_file="audio.srt", from_lang="en", to_lang="ru"):
    input_path = Path(input_file)
    if not input_path.exists():
        print(f"Файл {input_file} не найден!")
        return

    output_path = input_path.with_name(f"{input_path.stem}_{to_lang}.srt")

    existing_translations = {}
    if output_path.exists():
        print(f"Found existing translation file: {output_path.name}. Loading already translated blocks...")
        existing_translations = read_srt_file(output_path)

    subtiles = read_srt_file(input_path)

    len_subtiles = len(subtiles)
    flush_percent = max(1, len_subtiles // 20)
    count = 0

    outfile = open(output_path, "w", encoding="utf-8")

    for timestamp, text in subtiles.items(): 
        count += 1
        if count % flush_percent == 0:
            p = count * 100 / len_subtiles
            print(f"Processed {p:2.2f}% ({count}/{len_subtiles})")
            outfile.flush()

        # Check if already translated
        if timestamp in existing_translations:
            translated_text = existing_translations[timestamp]
        else:
            translated_text = translate_text(text, from_lang, to_lang)
        
        outfile.write(f'{count}\n')
        outfile.write(timestamp + '\n')
        outfile.write(translated_text + '\n')
        outfile.write('\n')

    outfile.close()

    print(f"\nDone!")
    print(f"Output file: {output_path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Use: translate.py path_to_sub.srt from_language(en) to_language(ru)")
        sys.exit(1)
    
    main(sys.argv[1], sys.argv[2], sys.argv[3])