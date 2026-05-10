# переводим srt файлы, оставляем те же временные метки

import os
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


def main(input_file="audio.srt", from_lang="en", to_lang="ru"):
    input_path = Path(input_file)
    if not input_path.exists():
        print(f"Файл {input_file} не найден!")
        return

    output_path = input_path.with_name(f"{input_path.stem}_{to_lang}.srt")

    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n(?=\d+\n)', content.strip() + '\n')

    srt_lines = []

    count_blocks = len(blocks)
    ten_percent = count_blocks // 10
    count = 0

    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        count += 1
        if count % ten_percent == 0:
            p = count * 100 / count_blocks
            print(f"Translated {p:2.2f}%")
        if len(lines) < 2:
            print("Error")
            continue

        srt_text = lines[2]
        if not srt_text:
            continue

        translated_text = translate_text(srt_text, from_lang, to_lang)

        srt_lines.append(lines[0])
        srt_lines.append(lines[1])
        srt_lines.append(translated_text)
        srt_lines.append('')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    print(f"\nDone!")
    print(f"Output file: {output_path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Use: translate.py path_to_sub.srt from_language(en) to_language(ru)")
        os.exit(1)
    
    main(sys.argv[1], sys.argv[2], sys.argv[3])